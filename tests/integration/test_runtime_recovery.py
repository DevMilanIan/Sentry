from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any
from uuid import UUID

import pytest
from pydantic import BaseModel

from app.clock.base import VirtualClock
from app.config import LoadedConfig, load_config
from app.db.repository import InMemoryAuditRepository
from app.demo.offline_scenario import _scripted_outputs
from app.demo.runtime import OfflineRuntimeSnapshot
from app.domain.enums import (
    ExecutionEnvironment,
    OrderSide,
    OrderState,
    RuntimeSafetyState,
    TradingMode,
)
from app.domain.models import BrokerCommandIntent, BrokerOrder, TradeProposal
from app.exceptions import SafetyCriticalError
from app.execution import ExecutionDenied
from app.execution.service import StateTransitionRecord
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
    await restored.offline.review_closed_positions()
    await restored.offline.review_closed_positions()
    outcomes = await repo.list_payloads(
        "trade_outcomes", filters={"record_kind": "closed_position_review"}
    )
    assert len(outcomes) == 1
    assert Decimal(outcomes[0]["payload"]["gross_realized_pnl"]) == Decimal("6")
    assert outcomes[0]["payload"]["net_realized_pnl"] is None
    assert outcomes[0]["payload"]["configuration_changes_applied"] is False
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


@pytest.mark.parametrize("failed_table", ["fills", "position_snapshots"])
async def test_fill_journal_failure_stays_unhealthy_until_reconciled_then_restores_exactly(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failed_table: str,
) -> None:
    loaded = settings(tmp_path)
    repo = InMemoryAuditRepository(loaded.bind_runtime())
    runtime = await build(loaded, repo)
    try:
        await ready(runtime)
        offline = runtime.offline
        assert offline is not None
        await add_entry(runtime)
        await offline.dispatch_proposals()
        previous_checkpoint = offline.session.checkpoint
        original_append = repo.append
        journal_unwritable = True

        async def append_with_journal_failure(
            table: str, value: BaseModel | Mapping[str, Any]
        ) -> UUID:
            if journal_unwritable and table == failed_table:
                raise OSError(f"injected {failed_table} write failure after ledger snapshot commit")
            return await original_append(table, value)

        monkeypatch.setattr(repo, "append", append_with_journal_failure)
        with pytest.raises(OSError, match="after ledger snapshot commit"):
            await offline.step()

        state = offline.broker.export_state()
        assert len(state.fills) == 1
        assert len(state.positions) == 1
        assert state.orders[0].published.state is OrderState.FILLED
        assert offline.broker.state_persisted
        row = await repo.find_payload(
            "shadow_ledger_events", "snapshot_kind", "offline-runtime-v1"
        )
        assert row is not None
        committed = OfflineRuntimeSnapshot.model_validate(
            {key: value for key, value in row["payload"].items() if key != "created_at"}
        )
        committed.validate_hash()
        assert committed.ledger == state
        assert committed.checkpoint == previous_checkpoint

        # Database and broker probes are healthy even though the execution
        # journal is incomplete; they must not clear the runtime failure latch.
        for _ in range(2):
            await runtime.controller.health_once()
            assert runtime.view.database_healthy
            assert runtime.view.broker_connected
            assert not runtime.view.execution_service_healthy
            assert not runtime.view.reconciled
            assert not await offline.health()
        assert not await runtime.controller.reconcile()
        assert not runtime.view.reconciled

        journal_unwritable = False
        await runtime.controller.health_once()
        assert not await offline.health()
        assert not runtime.view.reconciled
        with pytest.raises(SafetyCriticalError, match="initialized durable state"):
            await offline.step()
        assert not await offline.health()
        assert offline.session.checkpoint == previous_checkpoint
        assert await runtime.controller.reconcile()
        assert await offline.health()
        assert await offline.store.list_fills() == state.fills
        assert await offline.store.list_positions() == state.positions
        assert await offline.store.list_latest_orders() == tuple(
            item.published for item in state.orders
        )
    finally:
        await runtime.close()

    # Restart from the committed ledger plus the preceding replay checkpoint.
    # Reprocessing that quote must not apply its fill or liquidity a second time.
    restored = await build(loaded, repo)
    try:
        offline = restored.offline
        assert offline is not None
        assert offline.broker.export_state() == state
        assert await restored.controller.reconcile()
        await offline.step()
        assert offline.broker.export_state() == state
        assert await offline.store.list_fills() == state.fills
        assert await offline.store.list_positions() == state.positions
        assert await offline.store.list_latest_orders() == tuple(
            item.published for item in state.orders
        )
        assert len(await repo.list("fills")) == 1
        assert len(await repo.list("broker_command_intents")) == 1
    finally:
        await restored.close()


@pytest.mark.parametrize("persist_non_submission_evidence", [True, False])
async def test_local_rejection_requires_durable_proof_before_accepting_missing_ledger_order(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    persist_non_submission_evidence: bool,
) -> None:
    loaded = settings(tmp_path)
    repo = InMemoryAuditRepository(loaded.bind_runtime())
    runtime = await build(loaded, repo)
    try:
        await ready(runtime)
        offline = runtime.offline
        assert offline is not None
        proposal = await add_entry(runtime)
        original_save = offline.store.save_command_intent
        original_transition = offline.store.record_transition

        async def operator_changes_mode(command: BrokerCommandIntent) -> None:
            await original_save(command)
            runtime.view.trading_mode = TradingMode.RESEARCH

        async def persist_rejection_evidence(record: StateTransitionRecord) -> None:
            if record.current is OrderState.REJECTED and not persist_non_submission_evidence:
                raise OSError("injected rejection evidence write failure")
            await original_transition(record)

        monkeypatch.setattr(offline.store, "save_command_intent", operator_changes_mode)
        monkeypatch.setattr(offline.store, "record_transition", persist_rejection_evidence)
        error = ExecutionDenied if persist_non_submission_evidence else OSError
        with pytest.raises(error):
            await offline.execution.execute(proposal)
        orders = await offline.store.list_latest_orders()
        assert len(orders) == 1
        assert orders[0].state is OrderState.REJECTED
        assert await offline.broker.get_orders() == ()
        transitions = await offline.store.list_transitions(orders[0].intent_id)
        assert any("no broker write attempted" in item.reason for item in transitions) is (
            persist_non_submission_evidence
        )
        assert await runtime.controller.reconcile() is persist_non_submission_evidence
        if persist_non_submission_evidence:
            assert not await offline.store.unresolved_intents()
        else:
            assert not await offline.health()
            assert not runtime.view.reconciled
    finally:
        await runtime.close()

    restored = await build(loaded, repo)
    try:
        assert restored.offline is not None
        assert await restored.controller.reconcile() is persist_non_submission_evidence
        assert await restored.offline.store.list_latest_orders() == orders
        assert await restored.offline.broker.get_orders() == ()
    finally:
        await restored.close()


async def test_dispatch_reaches_pending_proposals_older_than_its_first_page(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    loaded = settings(tmp_path)
    repo = InMemoryAuditRepository(loaded.bind_runtime())
    runtime = await build(loaded, repo)
    try:
        await ready(runtime)
        offline = runtime.offline
        assert offline is not None
        original = await add_entry(runtime)
        proposals = [original]
        for number in range(1, 1203):
            proposal = original.model_copy(update={"proposal_id": UUID(int=number)})
            proposals.append(proposal)
            await repo.append("trade_proposals", proposal)
        visited: list[UUID] = []

        async def record_dispatch(proposal: TradeProposal) -> None:
            visited.append(proposal.proposal_id)

        monkeypatch.setattr(offline, "_dispatch", record_dispatch)
        await offline.dispatch_proposals()
        assert len(visited) == len(proposals)
        assert set(visited) == {proposal.proposal_id for proposal in proposals}
        assert original.proposal_id in visited
    finally:
        await runtime.close()
