"""Real pg_dump/pg_restore roundtrip using only two freshly owned databases.

This requires the same database-creation opt-in as the migration smoke test,
plus installed PostgreSQL client tools. Neither the configured database nor an
operational backup is read by pg_dump, restored, or removed by this test.
"""

from __future__ import annotations

import asyncio
import os
import re
import shutil
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.engine import URL, make_url
from sqlalchemy.ext.asyncio import create_async_engine

from app.broker.shadow_ledger import LedgerSnapshot
from app.clock.base import VirtualClock
from app.config import LoadedConfig, load_config
from app.db.models import ENVIRONMENT_TABLE_NAMES, SHARED_TABLE_NAMES
from app.db.repository import PostgresAuditRepository
from app.db.session import DatabaseManager
from app.demo.offline_scenario import _scripted_outputs
from app.domain.enums import (
    DemoBackend,
    ExecutionEnvironment,
    OrderSide,
    OrderState,
    RuntimeSafetyState,
    TradingMode,
)
from app.domain.models import TradeProposal
from app.main import _upgrade_database
from app.reasoning.scripted import ScriptedReplayModelProvider
from app.runtime import build_application

pytestmark = pytest.mark.integration
_DATABASE_NAME = re.compile(r"^sentry_restore_test_[0-9a-f]{32}_(source|target)$")
_INSTANT = datetime(2026, 9, 3, tzinfo=UTC)


@dataclass(frozen=True)
class BackupDatabases:
    source: URL = field(repr=False)
    target: URL = field(repr=False)
    pg_dump: str
    pg_restore: str
    token: str


def _client_binary(name: str) -> str:
    directory = os.environ.get("SENTRY_TEST_PG_BIN_DIR")
    if directory:
        suffix = ".exe" if os.name == "nt" else ""
        candidate = Path(directory) / f"{name}{suffix}"
        if candidate.is_file():
            return str(candidate.resolve())
    else:
        found = shutil.which(name)
        if found:
            return found
    pytest.fail(f"real backup/restore requires an installed {name} client", pytrace=False)


def _client_environment(url: URL) -> dict[str, str]:
    # Do not inherit arbitrary PGOPTIONS, PGSERVICE, PGPASSFILE, or broker secrets.
    environment = {
        key: value
        for key, value in os.environ.items()
        if key.upper() in {"PATH", "SYSTEMROOT", "WINDIR", "TEMP", "TMP", "LANG", "LC_ALL"}
    }
    environment.update(
        {
            "PGHOST": url.host or "localhost",
            "PGPORT": str(url.port or 5432),
            "PGUSER": url.username or "",
            "PGPASSWORD": url.password or "",
            "PGPASSFILE": os.devnull,
            "PGDATABASE": url.database or "",
            "PGCONNECT_TIMEOUT": "10",
            "PGAPPNAME": "sentry-isolated-backup-restore-test",
        }
    )
    return environment


async def _run_client(binary: str, arguments: list[str], url: URL) -> bytes:
    # No shell and no credential-bearing argv. Do not surface client stderr:
    # server errors can contain connection or database material.
    try:
        process = await asyncio.create_subprocess_exec(
            binary,
            *arguments,
            env=_client_environment(url),
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except OSError:
        raise RuntimeError("PostgreSQL verification client could not start") from None
    try:
        stdout, _ = await asyncio.wait_for(process.communicate(), timeout=60)
    except TimeoutError:
        process.terminate()
        try:
            await asyncio.wait_for(process.communicate(), timeout=5)
        except TimeoutError:
            process.kill()
            await process.communicate()
        raise RuntimeError("PostgreSQL verification client exceeded its timeout") from None
    if process.returncode != 0:
        raise RuntimeError("PostgreSQL verification client failed; raw output suppressed")
    return stdout


@pytest.fixture
async def backup_databases() -> AsyncIterator[BackupDatabases]:
    raw_url = os.environ.get("SENTRY_TEST_DATABASE_URL")
    if not raw_url or os.environ.get("SENTRY_TEST_ALLOW_DATABASE_CREATION") != "1":
        pytest.skip("real backup/restore requires a PostgreSQL URL and database-creation opt-in")
    try:
        admin_url = make_url(raw_url)
    except Exception:
        pytest.fail("SENTRY_TEST_DATABASE_URL is not a valid database URL", pytrace=False)
    if admin_url.drivername not in {"postgresql", "postgresql+asyncpg"} or admin_url.query:
        pytest.fail("backup/restore requires a PostgreSQL/asyncpg URL without query options")
    admin_url = admin_url.set(drivername="postgresql+asyncpg")
    pg_dump, pg_restore = _client_binary("pg_dump"), _client_binary("pg_restore")
    token = uuid4().hex
    names = tuple(f"sentry_restore_test_{token}_{suffix}" for suffix in ("source", "target"))
    marker = f"Sentry isolated backup restore test {token}"
    admin = create_async_engine(
        admin_url,
        isolation_level="AUTOCOMMIT",
        connect_args={"timeout": 10, "command_timeout": 30},
    )
    created: list[str] = []
    tagged: set[str] = set()
    try:
        async with admin.connect() as connection:
            server_version = int(str(await connection.scalar(text("SHOW server_version_num"))))
            server_major = server_version // 10000
            for binary in (pg_dump, pg_restore):
                version = await _run_client(binary, ["--version"], admin_url)
                match = re.search(rb"\(PostgreSQL\) (\d+)\.", version)
                if match is None or int(match[1]) < server_major:
                    pytest.fail("backup/restore requires clients at least as new as the server")
            for name in names:
                assert _DATABASE_NAME.fullmatch(name) and name != admin_url.database
                # CREATE without IF NOT EXISTS never adopts any pre-existing DB.
                await connection.execute(text(f'CREATE DATABASE "{name}"'))
                created.append(name)
                await connection.execute(text(f'COMMENT ON DATABASE "{name}" IS \'{marker}\''))
                tagged.add(name)
        yield BackupDatabases(
            admin_url.set(database=names[0]), admin_url.set(database=names[1]),
            pg_dump, pg_restore, token,
        )
    finally:
        try:
            async with admin.connect() as connection:
                for name in reversed(created):
                    if (
                        name not in tagged
                        or name not in names
                        or not _DATABASE_NAME.fullmatch(name)
                    ):
                        raise RuntimeError("refusing cleanup outside exactly tagged test databases")
                    found_marker = await connection.scalar(
                        text(
                            "SELECT shobj_description(oid, 'pg_database') "
                            "FROM pg_database WHERE datname = :name"
                        ),
                        {"name": name},
                    )
                    if found_marker != marker:
                        raise RuntimeError("refusing cleanup without exact test database ownership")
                    # An unexpected open session is a failure, never forcibly disconnected.
                    await connection.execute(text(f'DROP DATABASE "{name}"'))
        finally:
            await admin.dispose()


def _loaded(url: URL, token: str, tmp_path: Path) -> LoadedConfig:
    loaded = load_config()
    return loaded.model_copy(
        update={
            "app": loaded.app.model_copy(
                update={
                    "execution_environment": ExecutionEnvironment.DEMO,
                    "demo_backend": DemoBackend.OFFLINE_SIM,
                    "trading_mode": TradingMode.AUTO,
                    "database": loaded.app.database.model_copy(
                        update={"url": url.render_as_string(hide_password=False)}
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
                    "database_schema": "demo",
                    "runtime_directory": tmp_path / "demo",
                    "idempotency_namespace": f"backup-restore-test-{token}",
                }
            ),
        }
    )


async def _create_filled_ledger(loaded: LoadedConfig) -> LedgerSnapshot:
    manager = DatabaseManager(loaded.app.database.url, loaded.bind_runtime())
    runtime = None
    try:
        runtime = await build_application(
            loaded,
            repository=PostgresAuditRepository(manager),
            model_provider=ScriptedReplayModelProvider(_scripted_outputs()),
            wall_clock=VirtualClock(_INSTANT),
        )
        offline = runtime.offline
        assert offline is not None
        assert await runtime.controller.reconcile()
        await offline.step()
        await offline.step()
        await runtime.controller.health_once()
        await runtime.controller.health_once()
        assert runtime.view.safety.state is RuntimeSafetyState.NORMAL
        quote = await offline.session.market.get_option_quote("ACME-20260123-C-10.25")
        proposal = TradeProposal(
            created_at=offline.clock.now(),
            environment=ExecutionEnvironment.DEMO,
            namespace=loaded.bind_runtime().idempotency_namespace,
            packet_id=uuid4(),
            symbol=quote.contract.symbol,
            contract=quote.contract,
            side=OrderSide.BUY_TO_OPEN,
            quantity=1,
            limit_price=quote.ask,
            quote_snapshot_id=quote.snapshot_id,
            quote_as_of=quote.metadata.observed_at,
            policy_version="isolated-backup-restore-test",
            risk_config_version=loaded.risk.version,
            thesis="explicit isolated replay fixture entry",
            invalidation_conditions=("fixture invalidated",),
        )
        await offline.add_proposal(proposal)
        await offline.dispatch_proposals()
        assert offline.broker.export_state().orders[0].published.state is OrderState.OPEN
        await offline.step()
        ledger = offline.broker.export_state()
        assert len(ledger.fills) == len(ledger.positions) == 1
        assert ledger.orders[0].published.state is OrderState.FILLED
        return ledger
    finally:
        if runtime is not None:
            await runtime.close()
        await manager.close()


async def _audit_rows(url: URL) -> dict[str, list[tuple[object, ...]]]:
    engine = create_async_engine(url)
    rows: dict[str, list[tuple[object, ...]]] = {}
    try:
        async with engine.connect() as connection:
            for schema, tables in (
                ("shared", SHARED_TABLE_NAMES),
                ("demo", ENVIRONMENT_TABLE_NAMES),
                ("live", ENVIRONMENT_TABLE_NAMES),
            ):
                for table in tables:
                    result = await connection.execute(
                        # Both identifiers come only from the static schema/table catalog above.
                        text(f'SELECT * FROM "{schema}"."{table}" ORDER BY append_sequence')  # noqa: S608
                    )
                    rows[f"{schema}.{table}"] = [tuple(row) for row in result]
            revision = await connection.scalar(text("SELECT version_num FROM alembic_version"))
            rows["alembic_version"] = [(revision,)]
        return rows
    finally:
        await engine.dispose()


async def test_real_postgres_backup_restore_preserves_runtime_ledger_and_identity(
    backup_databases: BackupDatabases, tmp_path: Path,
) -> None:
    databases = backup_databases
    await asyncio.to_thread(
        _upgrade_database, databases.source.render_as_string(hide_password=False)
    )
    source_loaded = _loaded(databases.source, databases.token, tmp_path)
    ledger = await _create_filled_ledger(source_loaded)
    original_rows = await _audit_rows(databases.source)
    assert original_rows["demo.order_intents"] and original_rows["demo.broker_command_intents"]
    assert original_rows["demo.fills"] and original_rows["demo.shadow_ledger_events"]
    assert not any(rows for table, rows in original_rows.items() if table.startswith("live."))
    archive = tmp_path / "isolated-replay.dump"
    await _run_client(
        databases.pg_dump,
        ["--format=custom", "--no-owner", "--no-acl", "--no-password", f"--file={archive}"],
        databases.source,
    )
    assert archive.is_file() and archive.stat().st_size > 0
    await _run_client(
        databases.pg_restore,
        [
            "--exit-on-error", "--single-transaction", "--no-owner", "--no-acl", "--no-password",
            "--dbname", str(databases.target.database), str(archive),
        ],
        databases.target,
    )
    assert await _audit_rows(databases.target) == original_rows
    restored_loaded = _loaded(databases.target, databases.token, tmp_path)
    manager = DatabaseManager(restored_loaded.app.database.url, restored_loaded.bind_runtime())
    restored = None
    try:
        repository = PostgresAuditRepository(manager)
        restored = await build_application(
            restored_loaded,
            repository=repository,
            model_provider=ScriptedReplayModelProvider(_scripted_outputs()),
            wall_clock=VirtualClock(_INSTANT),
        )
        assert restored.offline is not None
        assert restored.offline.broker.export_state() == ledger
        assert await restored.controller.reconcile()
        assert await restored.offline.store.list_fills() == ledger.fills
        assert await restored.offline.store.list_positions() == ledger.positions
        assert await restored.offline.store.list_latest_orders() == tuple(
            item.published for item in ledger.orders
        )
        before = await repository.list_payloads("health_events")
        highest = max((row["append_sequence"] for row in before), default=0)
        await repository.append("health_events", {"created_at": _INSTANT, "test": "after restore"})
        after = await repository.list_payloads("health_events", limit=1)
        assert after[0]["append_sequence"] > highest
    finally:
        if restored is not None:
            await restored.close()
        await manager.close()


def test_pg_client_environment_excludes_unrelated_credentials(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("PGPASSWORD", "unrelated-pg-password")
    monkeypatch.setenv("PGSERVICE", "unrelated-service")
    monkeypatch.setenv("PGOPTIONS", "unrelated-options")
    monkeypatch.setenv("BROKER_TOKEN", "unrelated-broker-token")
    url = URL.create(
        "postgresql+asyncpg", username="isolated-role", password="test-password",
        host="test-host", port=5544, database="isolated-database",
    )
    environment = _client_environment(url)
    assert environment["PGPASSWORD"] == "test-password"
    assert environment["PGHOST"] == "test-host"
    assert environment["PGPORT"] == "5544"
    assert environment["PGDATABASE"] == "isolated-database"
    assert environment["PGUSER"] == "isolated-role"
    assert not {"PGOPTIONS", "PGSERVICE", "BROKER_TOKEN"} & environment.keys()


@pytest.mark.parametrize("returncode", [0, 1])
async def test_pg_client_never_places_credentials_in_argv_or_failure_output(
    monkeypatch: pytest.MonkeyPatch, returncode: int,
) -> None:
    captured: dict[str, object] = {}
    password = "private-test-password"

    class FakeProcess:
        returncode: int

        async def communicate(self) -> tuple[bytes, bytes]:
            return b"safe result", password.encode()

    process = FakeProcess()
    process.returncode = returncode

    async def create_process(*arguments: str, **keywords: object) -> FakeProcess:
        captured["arguments"] = arguments
        captured.update(keywords)
        return process

    monkeypatch.setattr(asyncio, "create_subprocess_exec", create_process)
    url = URL.create("postgresql+asyncpg", username="test", password=password, database="test")
    if returncode:
        with pytest.raises(RuntimeError, match="raw output suppressed") as raised:
            await _run_client("trusted-pg-dump", ["--format=custom"], url)
        assert password not in str(raised.value)
    else:
        assert await _run_client("trusted-pg-dump", ["--format=custom"], url) == b"safe result"
    assert captured["arguments"] == ("trusted-pg-dump", "--format=custom")
    assert password not in str(captured["arguments"])
    assert captured["stdin"] == asyncio.subprocess.DEVNULL
