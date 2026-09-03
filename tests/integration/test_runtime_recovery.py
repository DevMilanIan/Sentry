from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from uuid import UUID

import pytest

from app.clock.base import VirtualClock
from app.config import LoadedConfig, load_config
from app.db.repository import InMemoryAuditRepository
from app.demo.offline_scenario import _scripted_outputs
from app.domain.enums import (
    ExecutionEnvironment,
    OrderSide,
    OrderState,
    RuntimeSafetyState,
    TradingMode,
)
from app.domain.models import BrokerOrder, TradeProposal
from app.exceptions import SafetyCriticalError
from app.reasoning.scripted import ScriptedReplayModelProvider
from app.runtime import ApplicationRuntime, build_application


def settings(tmp_path: Path) -> LoadedConfig:
    loaded = load_config()
    runtime = loaded.app.runtime.model_copy(
        update={
            "environment_execution_disabled": False,
            "startup_health_window_seconds": 0,
            "disabled_file": tmp_path / "TRADING_DISABLED",
            "instance_lock_dir": tmp_path / "locks",
        }
    )
    return loaded.model_copy(
        update={
            "app": loaded.app.model_copy(
                update={
                    "runtime": runtime,
                    "trading_mode": TradingMode.AUTO,
                }
            )
        }
    )


async def build(loaded: LoadedConfig, repo: InMemoryAuditRepository) -> ApplicationRuntime:
    return await build_application(
        loaded,
        repository=repo,
        model_provider=ScriptedReplayModelProvider(_scripted_outputs()),
        wall_clock=VirtualClock(datetime(2026, 9, 3, tzinfo=UTC)),
    )


async def ready(runtime: ApplicationRuntime) -> None:
    assert runtime.offline is not None
    assert await runtime.controller.reconcile()
    await runtime.offline.step()
    await runtime.offline.step()
    await runtime.controller.health_once()
    await runtime.controller.health_once()
    assert runtime.view.safety.state is RuntimeSafetyState.NORMAL


async def add_entry(runtime: ApplicationRuntime) -> TradeProposal:
    offline = runtime.offline
    assert offline is not None
    quote = await offline.session.market.get_option_quote("ACME-20260123-C-10.25")
    proposal = TradeProposal(
        proposal_id=UUID("40000000-0000-4000-8000-000000000001"),
        created_at=offline.clock.now(),
        environment=ExecutionEnvironment.DEMO,
        namespace=runtime.view.binding.idempotency_namespace,
        packet_id=UUID("40000000-0000-4000-8000-000000000002"),
        symbol="ACME",
        contract=quote.contract,
        side=OrderSide.BUY_TO_OPEN,
        quantity=1,
        limit_price=quote.ask,
        quote_snapshot_id=quote.snapshot_id,
        quote_as_of=quote.metadata.observed_at,
        policy_version="test-approved-evidence",
        risk_config_version=runtime.loaded.risk.version,
        thesis="explicit fixture entry",
        invalidation_conditions=("fixture invalidated",),
    )
    await offline.add_proposal(proposal)
    return proposal


async def test_continuous_runtime_restores_open_order_and_finishes_exit(tmp_path: Path) -> None:
    loaded = settings(tmp_path)
    repo = InMemoryAuditRepository(loaded.bind_runtime())
    first = await build(loaded, repo)
    await ready(first)
    assert first.offline is not None
    await add_entry(first)
    await first.offline.dispatch_proposals()
    assert (await first.offline.broker.get_orders())[0].state is OrderState.OPEN
    first_hash = first.offline.broker.export_state().content_hash
    await first.close()

    restored = await build(loaded, repo)
    assert restored.offline is not None
    assert restored.offline.broker.export_state().content_hash == first_hash
    assert await restored.controller.reconcile()
    await restored.controller.health_once()
    await restored.controller.health_once()
    await restored.offline.step()
    assert (await restored.offline.broker.get_orders())[0].state is OrderState.FILLED
    assert len(await restored.offline.store.list_positions()) == 1
    await restored.offline.step()
    await restored.offline.dispatch_proposals()
    assert len(await restored.offline.broker.get_orders()) == 2
    await restored.offline.step()
    assert restored.offline.session.complete
    assert not restored.view.market_data_fresh
    assert not await restored.offline.store.list_positions()
    assert restored.offline.broker.ledger.cash == Decimal("31.00")
    assert restored.view.replay["live_market_data"] is False
    assert not restored.model_provider.calls  # type: ignore[attr-defined]
    await restored.close()


async def test_snapshot_recovers_crash_between_broker_and_order_journal(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    loaded = settings(tmp_path)
    repo = InMemoryAuditRepository(loaded.bind_runtime())
    runtime = await build(loaded, repo)
    await ready(runtime)
    offline = runtime.offline
    assert offline is not None
    proposal = await add_entry(runtime)
    original = offline.store.save_order

    async def fail_after_acceptance(order: BrokerOrder) -> None:
        if order.state is OrderState.OPEN:
            raise OSError("simulated process loss after broker snapshot commit")
        await original(order)

    monkeypatch.setattr(offline.store, "save_order", fail_after_acceptance)
    with pytest.raises(OSError):
        await offline.execution.execute(proposal)
    assert (await offline.store.list_latest_orders())[0].state is OrderState.SUBMITTING
    await runtime.close()
    restored = await build(loaded, repo)
    assert restored.offline is not None
    assert await restored.controller.reconcile()
    assert not await restored.offline.store.unresolved_intents()
    assert (await restored.offline.store.list_latest_orders())[0].state is OrderState.OPEN
    assert len(await repo.list("broker_command_intents")) == 1
    await restored.offline.step()
    assert len(await repo.list("fills")) == 1
    await restored.close()


async def test_corrupt_snapshot_cannot_reset_cash_or_resume(tmp_path: Path) -> None:
    loaded = settings(tmp_path)
    repo = InMemoryAuditRepository(loaded.bind_runtime())
    runtime = await build(loaded, repo)
    await ready(runtime)
    row = await repo.find_payload("shadow_ledger_events", "snapshot_kind", "offline-runtime-v1")
    assert row is not None
    payload = {**row["payload"], "ledger": {**row["payload"]["ledger"], "cash": "9999"}}
    await repo.append("shadow_ledger_events", payload)
    await runtime.close()
    with pytest.raises(SafetyCriticalError, match="checksum"):
        await build(loaded, repo)


async def test_database_failure_freezes_replay_and_halts_runtime(tmp_path: Path) -> None:
    loaded = settings(tmp_path)
    repo = InMemoryAuditRepository(loaded.bind_runtime())
    runtime = await build(loaded, repo)
    await ready(runtime)
    assert runtime.offline is not None
    before = runtime.offline.clock.now()
    repo.writable = False
    with pytest.raises(SafetyCriticalError):
        await runtime.offline.step()
    assert runtime.offline.clock.now() == before
    await runtime.controller.health_once()
    assert runtime.view.safety.state is RuntimeSafetyState.HALTED
    await runtime.close()
