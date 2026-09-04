"""Opt-in real PostgreSQL tests; never use the production schema names.

SENTRY_TEST_DATABASE_URL must reference a PostgreSQL database where the test
role may create schemas. Only fresh, tagged sentry_test_<uuid>_* schemas are
created and removed. This exercises metadata bootstrap, not Alembic upgrades.
"""

from __future__ import annotations

import asyncio
import os
import re
from collections.abc import AsyncIterator, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import pytest
from pydantic import BaseModel
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import create_async_engine

from app.clock.base import VirtualClock
from app.config import LoadedConfig, RuntimeBinding, load_config
from app.db.models import ENVIRONMENT_MODELS
from app.db.repository import PostgresAuditRepository
from app.db.session import DatabaseManager
from app.demo.offline_scenario import _scripted_outputs
from app.domain.enums import (
    BrokerAction,
    DemoBackend,
    ExecutionEnvironment,
    OrderSide,
    OrderState,
    RuntimeSafetyState,
    TradingMode,
)
from app.domain.models import OrderIntent, TradeProposal
from app.exceptions import SafetyCriticalError
from app.execution.postgres_store import PostgresExecutionStore
from app.execution.service import DuplicateOrderError
from app.reasoning.scripted import ScriptedReplayModelProvider
from app.runtime import ApplicationRuntime, build_application

pytestmark = pytest.mark.integration
INSTANT = datetime(2026, 9, 3, tzinfo=UTC)
_TEST_SCHEMA = re.compile(r"^sentry_test_[0-9a-f]{32}_(shared|demo|other)$")


@dataclass
class PostgresTestDatabase:
    url: str = field(repr=False)
    loaded: LoadedConfig = field(repr=False)
    shared_schema: str
    other_schema: str
    managers: list[DatabaseManager] = field(default_factory=list, repr=False)

    def manager(self, binding: RuntimeBinding | None = None) -> DatabaseManager:
        manager = DatabaseManager(
            self.url, binding or self.loaded.bind_runtime(), shared_schema=self.shared_schema
        )
        self.managers.append(manager)
        return manager


@pytest.fixture
async def postgres_database(tmp_path: Path) -> AsyncIterator[PostgresTestDatabase]:
    raw_url = os.environ.get("SENTRY_TEST_DATABASE_URL")
    if not raw_url:
        pytest.skip("SENTRY_TEST_DATABASE_URL is not configured; real PostgreSQL not tested")
    try:
        url = make_url(raw_url)
    except Exception:
        pytest.fail("SENTRY_TEST_DATABASE_URL is not a valid database URL", pytrace=False)
    if url.drivername not in {"postgresql", "postgresql+asyncpg"}:
        pytest.fail("SENTRY_TEST_DATABASE_URL must use PostgreSQL/asyncpg", pytrace=False)
    url = url.set(drivername="postgresql+asyncpg")
    connection_url = url.render_as_string(hide_password=False)
    token = uuid4().hex
    shared_schema = f"sentry_test_{token}_shared"
    environment_schema = f"sentry_test_{token}_demo"
    other_schema = f"sentry_test_{token}_other"
    expected_schemas = (shared_schema, environment_schema, other_schema)
    owner_marker = f"Sentry isolated integration test {token}"
    loaded = load_config()
    loaded = loaded.model_copy(
        update={
            "app": loaded.app.model_copy(
                update={
                    "execution_environment": ExecutionEnvironment.DEMO,
                    "demo_backend": DemoBackend.OFFLINE_SIM,
                    "trading_mode": TradingMode.AUTO,
                    "database": loaded.app.database.model_copy(
                        update={"url": connection_url, "shared_schema": shared_schema}
                    ),
                    "runtime": loaded.app.runtime.model_copy(
                        update={
                            "environment_execution_disabled": False,
                            "startup_health_window_seconds": 0,
                            "disabled_file": tmp_path / "TRADING_DISABLED",
                            "instance_lock_dir": tmp_path / "locks",
                        }
                    ),
                }
            ),
            "demo": loaded.demo.model_copy(
                update={
                    "database_schema": environment_schema,
                    "runtime_directory": tmp_path / "demo",
                    "idempotency_namespace": f"postgres-test-{token}",
                }
            ),
        }
    )
    database = PostgresTestDatabase(connection_url, loaded, shared_schema, other_schema)
    admin = create_async_engine(url, connect_args={"timeout": 10, "command_timeout": 30})
    created_schemas: list[str] = []
    try:
        # CREATE without IF NOT EXISTS proves these names were not pre-existing.
        # All three names commit together, so cleanup cannot adopt an old schema.
        async with admin.begin() as connection:
            for schema in expected_schemas:
                assert _TEST_SCHEMA.fullmatch(schema)
                await connection.execute(text(f'CREATE SCHEMA "{schema}"'))
                await connection.execute(
                    text(f'COMMENT ON SCHEMA "{schema}" IS \'{owner_marker}\'')
                )
        created_schemas.extend(expected_schemas)
        await database.manager().initialize_for_development()
        yield database
    finally:
        for manager in database.managers:
            await manager.close()
        try:
            async with admin.begin() as connection:
                for schema in reversed(created_schemas):
                    if schema not in expected_schemas or not _TEST_SCHEMA.fullmatch(schema):
                        raise RuntimeError("refusing cleanup outside exact test-owned schemas")
                    marker = await connection.scalar(
                        text(
                            "SELECT obj_description(oid, 'pg_namespace') "
                            "FROM pg_namespace WHERE nspname = :schema"
                        ),
                        {"schema": schema},
                    )
                    if marker != owner_marker:
                        raise RuntimeError("refusing cleanup of schema without test ownership tag")
                    await connection.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
        finally:
            await admin.dispose()


def make_intent(binding: RuntimeBinding) -> OrderIntent:
    return OrderIntent(
        created_at=INSTANT,
        environment=binding.environment,
        namespace=binding.idempotency_namespace,
        proposal_id=uuid4(),
        risk_decision_id=uuid4(),
        approval_id=None,
        review_id=uuid4(),
        order_fingerprint=f"fingerprint-{uuid4().hex}",
        idempotency_key=f"idempotency-{uuid4().hex}",
        action=BrokerAction.PLACE_OPTION_ORDER,
    )


async def test_real_postgres_schema_namespace_and_environment_isolation(
    postgres_database: PostgresTestDatabase,
) -> None:
    database = postgres_database
    binding = database.loaded.bind_runtime()
    first = PostgresAuditRepository(database.manager())
    namespace_peer = PostgresAuditRepository(
        database.manager(
            binding.model_copy(update={"idempotency_namespace": "other-test-namespace"})
        )
    )
    live_peer = PostgresAuditRepository(
        database.manager(
            binding.model_copy(
                update={"environment": ExecutionEnvironment.LIVE, "demo_backend": None}
            )
        )
    )
    other_manager = database.manager(
        binding.model_copy(update={"database_schema": database.other_schema})
    )
    await other_manager.initialize_for_development()
    schema_peer = PostgresAuditRepository(other_manager)
    repositories = (first, namespace_peer, live_peer, schema_peer)
    shared_identity = make_intent(first.binding)
    for number, repository in enumerate(repositories):
        await repository.append("health_events", {"created_at": INSTANT, "label": number})
        await repository.append(
            "order_intents",
            shared_identity.model_copy(
                update={
                    "environment": repository.binding.environment,
                    "namespace": repository.binding.idempotency_namespace,
                }
            ),
        )
    for number, repository in enumerate(repositories):
        rows = await repository.list_payloads("health_events")
        assert [row["payload"]["label"] for row in rows] == [number]
        assert len(await repository.list_payloads("order_intents")) == 1
    with pytest.raises(SafetyCriticalError, match="cross-environment"):
        await first.append("health_events", {"created_at": INSTANT, "environment": "LIVE"})
    with pytest.raises(SafetyCriticalError, match="cross-namespace"):
        await first.append("health_events", {"created_at": INSTANT, "namespace": "wrong"})


async def test_real_postgres_append_sequence_orders_equal_domain_times(
    postgres_database: PostgresTestDatabase,
) -> None:
    repository = PostgresAuditRepository(postgres_database.manager())
    for number in range(5):
        await repository.append("health_events", {"created_at": INSTANT, "label": number})
    first = await repository.list_payloads("health_events", limit=2)
    second = await repository.list_payloads(
        "health_events", limit=2, before_sequence=first[-1]["append_sequence"]
    )
    third = await repository.list_payloads(
        "health_events", limit=2, before_sequence=second[-1]["append_sequence"]
    )
    rows = first + second + third
    assert [row["payload"]["label"] for row in rows] == [4, 3, 2, 1, 0]
    sequences = [row["append_sequence"] for row in rows]
    assert all(isinstance(value, int) and value > 0 for value in sequences)
    assert sequences == sorted(set(sequences), reverse=True)
    assert {row["created_at"] for row in rows} == {INSTANT}
    latest = await repository.find_payload(
        "health_events", "created_at", INSTANT.isoformat().replace("+00:00", "Z")
    )
    assert latest is not None and latest["payload"]["label"] == 4


@pytest.mark.parametrize("collision_key", ["intent_id", "order_fingerprint", "idempotency_key"])
async def test_real_postgres_semantic_unique_indexes_reject_direct_collisions(
    postgres_database: PostgresTestDatabase,
    collision_key: str,
) -> None:
    repository = PostgresAuditRepository(postgres_database.manager())
    intent = make_intent(repository.binding)
    collision = make_intent(repository.binding).model_copy(
        update={collision_key: getattr(intent, collision_key)}
    )
    await repository.append("order_intents", intent)
    with pytest.raises(IntegrityError):
        await repository.append("order_intents", collision)
    assert len(await repository.list_payloads("order_intents")) == 1
    assert await repository.healthcheck()


class RendezvousRepository(PostgresAuditRepository):
    def __init__(self, manager: DatabaseManager, barrier: asyncio.Barrier) -> None:
        super().__init__(manager)
        self._barrier = barrier

    async def append(self, table: str, value: BaseModel | Mapping[str, Any]) -> UUID:
        if table == "order_intents":
            await self._barrier.wait()
        return await super().append(table, value)


@pytest.mark.parametrize("identical", [True, False])
async def test_real_postgres_concurrent_immutable_saves_rely_on_unique_indexes(
    postgres_database: PostgresTestDatabase,
    identical: bool,
) -> None:
    barrier = asyncio.Barrier(2)
    left = RendezvousRepository(postgres_database.manager(), barrier)
    right = RendezvousRepository(postgres_database.manager(), barrier)
    intent = make_intent(left.binding)
    other = intent if identical else make_intent(left.binding).model_copy(
        update={"idempotency_key": intent.idempotency_key}
    )
    outcomes = await asyncio.wait_for(
        asyncio.gather(
            PostgresExecutionStore(left, VirtualClock(INSTANT)).save_order_intent(intent),
            PostgresExecutionStore(right, VirtualClock(INSTANT)).save_order_intent(other),
            return_exceptions=True,
        ),
        timeout=30,
    )
    if identical:
        assert all(value is None for value in outcomes)
    else:
        assert sum(value is None for value in outcomes) == 1
        assert sum(isinstance(value, DuplicateOrderError) for value in outcomes) == 1
    rows = await left.list_payloads("order_intents")
    assert len(rows) == 1
    assert await left.healthcheck()
    assert await right.healthcheck()


async def test_real_postgres_health_probe_and_failed_application_transaction_roll_back(
    postgres_database: PostgresTestDatabase,
) -> None:
    manager = postgres_database.manager()
    repository = PostgresAuditRepository(manager)
    for _ in range(3):
        assert await manager.healthcheck()
    assert await repository.list_payloads("health_events") == []
    model = ENVIRONMENT_MODELS["health_events"]
    with pytest.raises(RuntimeError, match="test transaction rollback"):
        async with manager.session() as session:
            session.add(
                model(
                    id=uuid4(),
                    created_at=INSTANT,
                    run_id=None,
                    record_type="TestTransactionRollback",
                    environment=manager.binding.environment.value,
                    namespace=manager.binding.idempotency_namespace,
                    payload={"test": "rolled back"},
                )
            )
            await session.flush()
            raise RuntimeError("test transaction rollback")
    assert await repository.list_payloads("health_events") == []
    assert await manager.healthcheck()


async def test_real_postgres_readonly_connection_passes_select_but_fails_write_health(
    postgres_database: PostgresTestDatabase,
) -> None:
    manager = postgres_database.manager()
    manager.engine = manager.engine.execution_options(
        isolation_level="SERIALIZABLE", postgresql_readonly=True
    )
    async with manager.engine.connect() as connection:
        assert await connection.scalar(text("SELECT 1")) == 1
        assert await connection.scalar(text("SHOW transaction_read_only")) == "on"
    assert not await manager.healthcheck()
    writable = postgres_database.manager()
    assert await writable.healthcheck()
    assert await PostgresAuditRepository(writable).list_payloads("health_events") == []


async def build_runtime(database: PostgresTestDatabase) -> ApplicationRuntime:
    return await build_application(
        database.loaded,
        repository=PostgresAuditRepository(database.manager()),
        model_provider=ScriptedReplayModelProvider(_scripted_outputs()),
        wall_clock=VirtualClock(INSTANT),
    )


async def test_real_postgres_runtime_restart_uses_fresh_connections_and_exact_ledger(
    postgres_database: PostgresTestDatabase,
) -> None:
    first = await build_runtime(postgres_database)
    try:
        offline = first.offline
        assert offline is not None
        assert await first.controller.reconcile()
        await offline.step()
        await offline.step()
        await first.controller.health_once()
        await first.controller.health_once()
        assert first.view.safety.state is RuntimeSafetyState.NORMAL
        quote = await offline.session.market.get_option_quote("ACME-20260123-C-10.25")
        proposal = TradeProposal(
            created_at=offline.clock.now(),
            environment=ExecutionEnvironment.DEMO,
            namespace=first.view.binding.idempotency_namespace,
            packet_id=uuid4(),
            symbol=quote.contract.symbol,
            contract=quote.contract,
            side=OrderSide.BUY_TO_OPEN,
            quantity=1,
            limit_price=quote.ask,
            quote_snapshot_id=quote.snapshot_id,
            quote_as_of=quote.metadata.observed_at,
            policy_version="postgres-restart-test",
            risk_config_version=first.loaded.risk.version,
            thesis="explicit isolated replay fixture entry",
            invalidation_conditions=("fixture invalidated",),
        )
        await offline.add_proposal(proposal)
        await offline.dispatch_proposals()
        open_state = offline.broker.export_state()
        assert open_state.orders[0].published.state is OrderState.OPEN
    finally:
        await first.close()
        for manager in postgres_database.managers:
            await manager.close()

    second = await build_runtime(postgres_database)
    try:
        assert second.offline is not None
        assert second.offline.broker.export_state() == open_state
        assert await second.controller.reconcile()
        await second.offline.step()
        filled_state = second.offline.broker.export_state()
        assert len(filled_state.fills) == 1
        assert len(filled_state.positions) == 1
        assert filled_state.orders[0].published.state is OrderState.FILLED
    finally:
        await second.close()
        for manager in postgres_database.managers:
            await manager.close()

    third = await build_runtime(postgres_database)
    try:
        assert third.offline is not None
        assert third.offline.broker.export_state() == filled_state
        assert await third.controller.reconcile()
        assert await third.offline.store.list_fills() == filled_state.fills
        assert await third.offline.store.list_positions() == filled_state.positions
        assert await third.offline.store.list_latest_orders() == tuple(
            item.published for item in filled_state.orders
        )
        assert len(await third.repository.list_payloads("broker_command_intents")) == 1
    finally:
        await third.close()
