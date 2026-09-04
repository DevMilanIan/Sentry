from __future__ import annotations

import asyncio
from collections.abc import Mapping
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any
from uuid import UUID, uuid4

import pytest
from pydantic import BaseModel
from sqlalchemy.exc import IntegrityError

from app.clock.base import VirtualClock
from app.config import RuntimeBinding
from app.db.repository import InMemoryAuditRepository
from app.domain.enums import OrderSide, OrderState
from app.domain.models import BrokerOrder, Fill, TradeProposal
from app.exceptions import DataInvalidError, SafetyCriticalError
from app.learning.outcomes import ClosedPositionReviewWorker


@pytest.mark.parametrize(
    "bounds",
    [
        {"maximum_records": True},
        {"maximum_records": 1.5},
        {"maximum_records": 0},
        {"maximum_records": 10000},
        {"maximum_seconds": True},
        {"maximum_seconds": float("nan")},
        {"maximum_seconds": float("inf")},
        {"maximum_seconds": float("-inf")},
        {"maximum_seconds": 0},
    ],
)
def test_review_bounds_require_finite_non_boolean_values(
    demo_binding: RuntimeBinding, instant: datetime, bounds: dict[str, Any]
) -> None:
    with pytest.raises(ValueError, match="review bounds"):
        ClosedPositionReviewWorker(
            InMemoryAuditRepository(demo_binding), VirtualClock(instant), **bounds
        )


async def _fill(
    repository: InMemoryAuditRepository,
    proposal: TradeProposal,
    when: datetime,
    side: OrderSide,
    *,
    quantity: int = 1,
    price: str = "0.10",
    order: BrokerOrder | None = None,
) -> tuple[BrokerOrder, Fill]:
    if order is None:
        order = BrokerOrder(
            created_at=when,
            intent_id=uuid4(),
            environment=repository.binding.environment,
            state=OrderState.FILLED,
            contract=proposal.contract,
            side=side,
            quantity=quantity,
            filled_quantity=quantity,
            limit_price=Decimal(price),
            submitted_at=when,
        )
        await repository.append("orders", order)
    fill = Fill(
        created_at=when,
        order_id=order.order_id,
        quantity=quantity,
        price=Decimal(price),
        market_event_ids=("fixture-event",),
        fill_model_version="fixture-fill-v1",
        deterministic_seed=0,
        reason="fixture-only",
    )
    await repository.append("fills", fill)
    return order, fill


async def _roundtrip(
    repository: InMemoryAuditRepository,
    proposal: TradeProposal,
    instant: datetime,
) -> tuple[Fill, Fill]:
    _, entry = await _fill(repository, proposal, instant, OrderSide.BUY_TO_OPEN, quantity=2)
    _, exit_fill = await _fill(
        repository,
        proposal,
        instant + timedelta(seconds=65),
        OrderSide.SELL_TO_CLOSE,
        quantity=2,
        price="0.15",
    )
    return entry, exit_fill


async def test_closed_roundtrip_has_decimal_gross_statistics_and_explicit_unknowns(
    demo_binding: RuntimeBinding,
    proposal: TradeProposal,
    instant: datetime,
) -> None:
    repository = InMemoryAuditRepository(demo_binding)
    entry, exit_fill = await _roundtrip(repository, proposal, instant)
    clock = VirtualClock(instant + timedelta(minutes=2))
    outcomes = await ClosedPositionReviewWorker(repository, clock).tick()
    assert len(outcomes) == 1
    outcome = outcomes[0]
    assert outcome.gross_entry_cost == Decimal("20")
    assert outcome.gross_exit_proceeds == Decimal("30")
    assert outcome.gross_realized_pnl == Decimal("10")
    assert outcome.gross_return_percent == Decimal("50")
    assert outcome.hold_seconds == Decimal("65")
    assert outcome.entry_fill_ids == (entry.fill_id,)
    assert outcome.exit_fill_ids == (exit_fill.fill_id,)
    assert outcome.contracts_opened == outcome.contracts_closed == 2
    assert outcome.created_at == exit_fill.created_at
    assert outcome.reviewed_at == clock.now()
    assert outcome.fees is outcome.net_realized_pnl is outcome.maximum_adverse_excursion is None
    assert outcome.maximum_favorable_excursion is outcome.catalyst_assessment is None
    assert not outcome.configuration_changes_applied
    assert len(outcome.source_evidence_hash) == 64


async def test_partial_close_waits_for_flat_position_and_restart_deduplicates(
    demo_binding: RuntimeBinding,
    proposal: TradeProposal,
    instant: datetime,
) -> None:
    repository = InMemoryAuditRepository(demo_binding)
    clock = VirtualClock(instant + timedelta(minutes=5))
    await _fill(repository, proposal, instant, OrderSide.BUY_TO_OPEN, quantity=2)
    await _fill(repository, proposal, instant + timedelta(seconds=1), OrderSide.SELL_TO_CLOSE)
    worker = ClosedPositionReviewWorker(repository, clock)
    assert await worker.tick() == ()
    await _fill(
        repository, proposal, instant + timedelta(seconds=2), OrderSide.SELL_TO_CLOSE, price="0.20"
    )
    outcome = (await worker.tick())[0]
    assert len(outcome.exit_fill_ids) == 2
    assert outcome.gross_realized_pnl == Decimal("10")
    assert await worker.tick() == ()
    await clock.advance(timedelta(minutes=1))
    assert await ClosedPositionReviewWorker(repository, clock).tick() == ()
    assert len(await repository.list("trade_outcomes")) == 1


async def test_reopen_produces_distinct_flat_to_flat_cycles(
    demo_binding: RuntimeBinding,
    proposal: TradeProposal,
    instant: datetime,
) -> None:
    repository = InMemoryAuditRepository(demo_binding)
    await _roundtrip(repository, proposal, instant)
    await _roundtrip(repository, proposal, instant + timedelta(minutes=2))
    outcomes = await ClosedPositionReviewWorker(
        repository, VirtualClock(instant + timedelta(minutes=4))
    ).tick()
    assert len(outcomes) == 2
    assert outcomes[0].cycle_id != outcomes[1].cycle_id
    assert outcomes[0].outcome_id != outcomes[1].outcome_id


async def test_same_fill_duplicate_is_deduplicated_but_changed_identity_is_rejected(
    demo_binding: RuntimeBinding,
    proposal: TradeProposal,
    instant: datetime,
) -> None:
    repository = InMemoryAuditRepository(demo_binding)
    entry, _ = await _roundtrip(repository, proposal, instant)
    await repository.append("fills", entry)
    worker = ClosedPositionReviewWorker(repository, VirtualClock(instant + timedelta(minutes=4)))
    assert len(await worker.tick()) == 1
    await repository.append("fills", entry.model_copy(update={"price": Decimal("0.11")}))
    with pytest.raises(DataInvalidError, match="fill identity"):
        await worker.tick()
    assert len(await repository.list("trade_outcomes")) == 1


async def test_missing_order_and_oversell_fail_before_any_outcome_append(
    demo_binding: RuntimeBinding,
    proposal: TradeProposal,
    instant: datetime,
) -> None:
    repository = InMemoryAuditRepository(demo_binding)
    await _roundtrip(repository, proposal, instant)
    _, orphan = await _fill(
        repository, proposal, instant + timedelta(minutes=2), OrderSide.SELL_TO_CLOSE
    )
    worker = ClosedPositionReviewWorker(repository, VirtualClock(instant + timedelta(minutes=4)))
    with pytest.raises(DataInvalidError, match="oversells"):
        await worker.tick()
    assert await repository.list("trade_outcomes") == []
    repository._rows["orders"] = [
        row
        for row in repository._rows["orders"]
        if row["payload"]["order_id"] != str(orphan.order_id)
    ]
    with pytest.raises(DataInvalidError, match="no causally available"):
        await worker.tick()


async def test_inconsistent_contract_and_order_identities_are_rejected(
    demo_binding: RuntimeBinding,
    proposal: TradeProposal,
    instant: datetime,
) -> None:
    repository = InMemoryAuditRepository(demo_binding)
    order, _ = await _fill(repository, proposal, instant, OrderSide.BUY_TO_OPEN)
    await repository.append("orders", order.model_copy(update={"side": OrderSide.SELL_TO_CLOSE}))
    with pytest.raises(DataInvalidError, match="inconsistent exact terms"):
        await ClosedPositionReviewWorker(repository, VirtualClock(instant)).tick()
    repository = InMemoryAuditRepository(demo_binding)
    await _fill(repository, proposal, instant, OrderSide.BUY_TO_OPEN)
    changed = proposal.model_copy(
        update={"contract": proposal.contract.model_copy(update={"multiplier": 10})}
    )
    await _fill(repository, changed, instant, OrderSide.SELL_TO_CLOSE)
    with pytest.raises(DataInvalidError, match="inconsistent contract terms"):
        await ClosedPositionReviewWorker(repository, VirtualClock(instant)).tick()


async def test_cross_namespace_repository_result_is_rejected(
    demo_binding: RuntimeBinding,
    proposal: TradeProposal,
    instant: datetime,
) -> None:
    repository = InMemoryAuditRepository(demo_binding)
    await _roundtrip(repository, proposal, instant)
    original = repository.list_payloads

    async def corrupted(table: str, **kwargs: Any) -> list[dict[str, Any]]:
        rows = await original(table, **kwargs)
        if rows and table == "fills":
            rows[0] = {**rows[0], "namespace": "live-other-account"}
        return rows

    repository.list_payloads = corrupted  # type: ignore[method-assign]
    with pytest.raises(SafetyCriticalError, match="namespace"):
        await ClosedPositionReviewWorker(
            repository, VirtualClock(instant + timedelta(minutes=2))
        ).tick()
    assert await repository.list("trade_outcomes") == []


async def test_future_exit_is_not_consumed_until_clock_reaches_it(
    demo_binding: RuntimeBinding,
    proposal: TradeProposal,
    instant: datetime,
) -> None:
    repository = InMemoryAuditRepository(demo_binding)
    await _roundtrip(repository, proposal, instant)
    clock = VirtualClock(instant)
    worker = ClosedPositionReviewWorker(repository, clock)
    assert await worker.tick() == ()
    await clock.advance(timedelta(seconds=65))
    assert len(await worker.tick()) == 1


async def test_journal_fill_total_must_match_recorded_broker_quantity(
    demo_binding: RuntimeBinding,
    proposal: TradeProposal,
    instant: datetime,
) -> None:
    repository = InMemoryAuditRepository(demo_binding)
    order, _ = await _fill(repository, proposal, instant, OrderSide.BUY_TO_OPEN)
    await repository.append(
        "orders", order.model_copy(update={"quantity": 2, "filled_quantity": 2})
    )
    # A direct mutation mimics incomplete retained evidence, not a normal store write.
    repository._rows["orders"] = repository._rows["orders"][-1:]
    with pytest.raises(DataInvalidError, match="do not reconcile"):
        await ClosedPositionReviewWorker(repository, VirtualClock(instant)).tick()


@pytest.mark.parametrize("table", ["orders", "fills", "trade_outcomes"])
async def test_scan_bound_rejects_incomplete_evidence_without_appending(
    demo_binding: RuntimeBinding,
    proposal: TradeProposal,
    instant: datetime,
    table: str,
) -> None:
    repository = InMemoryAuditRepository(demo_binding)
    await _roundtrip(repository, proposal, instant)
    clock = VirtualClock(instant + timedelta(minutes=2))
    if table == "trade_outcomes":
        outcome = (await ClosedPositionReviewWorker(repository, clock).tick())[0]
        await repository.append("trade_outcomes", outcome)
    original = repository.list_payloads

    async def only_bounded(table_name: str, **kwargs: Any) -> list[dict[str, Any]]:
        rows = await original(table_name, **kwargs)
        return rows if table_name == table else rows[:1]

    repository.list_payloads = only_bounded  # type: ignore[method-assign]
    before = len(await repository.list("trade_outcomes"))
    with pytest.raises(DataInvalidError, match=f"scan incomplete: {table}"):
        await ClosedPositionReviewWorker(repository, clock, maximum_records=1).tick()
    assert len(await repository.list("trade_outcomes")) == before


async def test_ambiguous_post_commit_failure_is_restart_safe(
    demo_binding: RuntimeBinding,
    proposal: TradeProposal,
    instant: datetime,
) -> None:
    class AmbiguousRepository(InMemoryAuditRepository):
        failed = False

        async def append(self, table: str, value: BaseModel | Mapping[str, Any]) -> UUID:
            row_id = await super().append(table, value)
            if table == "trade_outcomes" and not self.failed:
                self.failed = True
                raise ConnectionError("fixture: response lost after commit")
            return row_id

    repository = AmbiguousRepository(demo_binding)
    await _roundtrip(repository, proposal, instant)
    clock = VirtualClock(instant + timedelta(minutes=2))
    with pytest.raises(ConnectionError):
        await ClosedPositionReviewWorker(repository, clock).tick()
    assert await ClosedPositionReviewWorker(repository, clock).tick() == ()
    assert len(await repository.list("trade_outcomes")) == 1


async def test_unique_index_race_accepts_only_identical_committed_outcome(
    demo_binding: RuntimeBinding,
    proposal: TradeProposal,
    instant: datetime,
) -> None:
    class RaceRepository(InMemoryAuditRepository):
        async def append(self, table: str, value: BaseModel | Mapping[str, Any]) -> UUID:
            row_id = await super().append(table, value)
            if table == "trade_outcomes":
                raise IntegrityError("fixture unique index", None, Exception("fixture"))
            return row_id

    repository = RaceRepository(demo_binding)
    await _roundtrip(repository, proposal, instant)
    assert (
        await ClosedPositionReviewWorker(
            repository, VirtualClock(instant + timedelta(minutes=2))
        ).tick()
        == ()
    )
    assert len(await repository.list("trade_outcomes")) == 1


async def test_concurrent_ticks_are_serialized_and_zero_trade_journal_is_quiet(
    demo_binding: RuntimeBinding,
    proposal: TradeProposal,
    instant: datetime,
) -> None:
    repository = InMemoryAuditRepository(demo_binding)
    worker = ClosedPositionReviewWorker(repository, VirtualClock(instant + timedelta(minutes=2)))
    assert await worker.tick() == ()
    assert await repository.list("trade_outcomes") == []
    await _roundtrip(repository, proposal, instant)
    results = await asyncio.gather(worker.tick(), worker.tick(), worker.tick())
    assert sum(len(result) for result in results) == 1
    assert len(await repository.list("trade_outcomes")) == 1


async def test_late_conflicting_fill_cannot_silently_invalidate_an_immutable_outcome(
    demo_binding: RuntimeBinding,
    proposal: TradeProposal,
    instant: datetime,
) -> None:
    repository = InMemoryAuditRepository(demo_binding)
    await _roundtrip(repository, proposal, instant)
    worker = ClosedPositionReviewWorker(repository, VirtualClock(instant + timedelta(minutes=2)))
    assert len(await worker.tick()) == 1
    await _fill(repository, proposal, instant + timedelta(seconds=1), OrderSide.BUY_TO_OPEN)
    with pytest.raises(DataInvalidError, match="not reproduced"):
        await worker.tick()
    assert len(await repository.list("trade_outcomes")) == 1


async def test_timeout_is_visible_and_does_not_write_outcomes(
    demo_binding: RuntimeBinding,
    instant: datetime,
) -> None:
    class SlowRepository(InMemoryAuditRepository):
        async def list_payloads(self, table: str, **kwargs: Any) -> list[dict[str, Any]]:
            await asyncio.sleep(1)
            return []

    repository = SlowRepository(demo_binding)
    with pytest.raises(TimeoutError):
        await ClosedPositionReviewWorker(
            repository, VirtualClock(instant), maximum_seconds=0.01
        ).tick()
    assert await repository.list("trade_outcomes") == []
