"""Opt-in current-read surveillance checks using only tagged disposable PG schemas."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any
from uuid import UUID, uuid4

import pytest
from pydantic import BaseModel
from test_live_surveillance_wiring import SyntheticCurrentProvider
from test_postgres_live import PostgresTestDatabase
from test_postgres_live import postgres_database as _postgres_database

from app.clock.base import VirtualClock
from app.db.repository import PostgresAuditRepository
from app.domain.enums import DemoBackend
from app.domain.models import TradeProposal
from app.exceptions import TransientError
from app.sentinel.live_reads import LiveReadSurveillanceWorker

pytestmark = pytest.mark.integration
postgres_database = _postgres_database


class InterruptedEventRepository(PostgresAuditRepository):
    failed = False
    after_commit = False

    async def append(self, table: str, value: BaseModel | Mapping[str, Any]) -> UUID:
        if table == "sentinel_events" and not self.failed:
            self.failed = True
            if self.after_commit:
                await super().append(table, value)
            raise ConnectionError("synthetic connection interrupted at event commit boundary")
        return await super().append(table, value)


@pytest.mark.parametrize("after_commit", [False, True])
async def test_postgres_new_pool_repairs_snapshot_event_gap_after_provider_changes(
    postgres_database: PostgresTestDatabase,
    proposal: TradeProposal,
    instant: datetime,
    after_commit: bool,
) -> None:
    database = postgres_database
    binding = database.loaded.bind_runtime().model_copy(
        update={"demo_backend": DemoBackend.BROKER_SHADOW}
    )
    first_manager = database.manager(binding)
    audit = InterruptedEventRepository(first_manager)
    audit.after_commit = after_commit
    clock = VirtualClock(instant)
    provider = SyntheticCurrentProvider(proposal, instant)
    first = LiveReadSurveillanceWorker(provider, clock, audit, watchlist=("TEST",))
    with pytest.raises(TransientError):
        await first.tick()
    snapshots = await audit.list_payloads(
        "market_snapshots", filters={"namespace": binding.idempotency_namespace}
    )
    assert len(snapshots) == 1
    first_payload = snapshots[0]["payload"]
    first_event_id = first_payload["event"]["event_id"]
    assert bool(await audit.list_payloads("sentinel_events")) is after_commit
    await first_manager.close()

    # The prior quote is no longer returned by this synthetic provider. Only durable
    # database contents can repair/re-offer the original event after process restart.
    await clock.advance(timedelta(seconds=5))
    metadata = provider.equity.metadata.model_copy(
        update={
            "effective_at": clock.now(),
            "observed_at": clock.now(),
        }
    )
    provider.equity = provider.equity.model_copy(
        update={
            "snapshot_id": uuid4(),
            "metadata": metadata,
            "last": Decimal("11"),
            "bid": Decimal("11"),
            "ask": Decimal("11.01"),
        }
    )
    provider.option = provider.option.model_copy(update={"metadata": metadata})
    restored_manager = database.manager(binding)
    assert restored_manager.engine is not first_manager.engine
    restored_audit = PostgresAuditRepository(restored_manager)
    restored = LiveReadSurveillanceWorker(provider, clock, restored_audit, watchlist=("TEST",))
    events = await restored.tick()
    assert first_event_id in {str(event.event_id) for event in events}
    assert any(event.event_type == "MARKET_MEASURED_CHANGE" for event in events)
    durable_events = await restored_audit.list_payloads("sentinel_events")
    keys = [row["payload"]["deduplication_key"] for row in durable_events]
    assert len(keys) == len(set(keys))
    saved_first = await restored_audit.find_payload("sentinel_events", "event_id", first_event_id)
    assert saved_first is not None
    assert saved_first["payload"]["raw_reference_ids"] == [first_payload["quote"]["snapshot_id"]]
    assert (
        saved_first["payload"]["effective_at"] == first_payload["quote"]["metadata"]["effective_at"]
    )
    assert (
        len(
            await restored_audit.list_payloads(
                "market_snapshots", filters={"namespace": binding.idempotency_namespace}
            )
        )
        == 2
    )
    assert await restored.tick() == ()
    assert len(await restored_audit.list_payloads("sentinel_events")) == len(durable_events)
    assert await restored.health()


async def test_postgres_shared_snapshot_envelopes_are_filtered_by_namespace(
    postgres_database: PostgresTestDatabase,
    proposal: TradeProposal,
    instant: datetime,
) -> None:
    database = postgres_database
    binding = database.loaded.bind_runtime().model_copy(
        update={"demo_backend": DemoBackend.BROKER_SHADOW}
    )
    other_binding = binding.model_copy(
        update={
            "idempotency_namespace": binding.idempotency_namespace + "-other",
        }
    )
    audit = PostgresAuditRepository(database.manager(binding))
    other = PostgresAuditRepository(database.manager(other_binding))
    clock = VirtualClock(instant)
    provider = SyntheticCurrentProvider(proposal, instant)
    original = await LiveReadSurveillanceWorker(provider, clock, audit, watchlist=("TEST",)).tick()
    source_row = (await audit.list_payloads("market_snapshots"))[0]["payload"]
    # A shared table has no repository-level namespace filter. Even if a foreign
    # envelope repeats another worker's key, the worker must explicitly exclude it.
    await other.append(
        "market_snapshots",
        {
            **source_row,
            "namespace": other_binding.idempotency_namespace,
        },
    )
    restored = LiveReadSurveillanceWorker(provider, clock, audit, watchlist=("TEST",))
    repeated = await restored.tick()
    assert {event.event_id for event in repeated} == {event.event_id for event in original}
    assert len(await audit.list_payloads("sentinel_events")) == 2
    assert not await other.list_payloads("sentinel_events")
    foreign = await LiveReadSurveillanceWorker(provider, clock, other, watchlist=("TEST",)).tick()
    assert len(foreign) == 2
    assert {event.event_id for event in foreign}.isdisjoint(event.event_id for event in original)
    assert len(await audit.list_payloads("sentinel_events")) == 2
    assert len(await other.list_payloads("sentinel_events")) == 2


async def test_postgres_snapshot_ids_remain_stable_across_adapter_ids_and_new_pools(
    postgres_database: PostgresTestDatabase,
    proposal: TradeProposal,
    instant: datetime,
) -> None:
    database = postgres_database
    binding = database.loaded.bind_runtime().model_copy(
        update={"demo_backend": DemoBackend.BROKER_SHADOW}
    )
    first_manager = database.manager(binding)
    audit = PostgresAuditRepository(first_manager)
    clock = VirtualClock(instant)
    provider = SyntheticCurrentProvider(proposal, instant)
    original = await LiveReadSurveillanceWorker(provider, clock, audit, watchlist=("TEST",)).tick()
    original_ids = {event.event_id for event in original}
    await first_manager.close()
    provider.equity = provider.equity.model_copy(update={"snapshot_id": uuid4()})
    provider.option = provider.option.model_copy(update={"snapshot_id": uuid4()})
    restored_audit = PostgresAuditRepository(database.manager(binding))
    restored = LiveReadSurveillanceWorker(provider, clock, restored_audit, watchlist=("TEST",))
    assert {event.event_id for event in await restored.tick()} == original_ids
    assert len(await restored_audit.list_payloads("market_snapshots")) == 1
    assert len(await restored_audit.list_payloads("option_snapshots")) == 1
    assert len(await restored_audit.list_payloads("sentinel_events")) == 2
