"""Real PostgreSQL outcome checks using fresh, tagged, fixture-owned schemas only."""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any
from uuid import UUID, uuid4

import pytest
from pydantic import BaseModel
from sqlalchemy.exc import IntegrityError
from test_postgres_live import PostgresTestDatabase
from test_postgres_live import postgres_database as _postgres_database

from app.clock.base import VirtualClock
from app.db.repository import PostgresAuditRepository
from app.db.session import DatabaseManager
from app.domain.enums import OptionType, OrderSide, OrderState
from app.domain.models import BrokerOrder, Fill, OptionContract
from app.learning.outcomes import ClosedPositionReviewWorker

pytestmark = pytest.mark.integration
postgres_database = _postgres_database
INSTANT = datetime(2026, 9, 4, 14, tzinfo=UTC)


async def _persist_synthetic_roundtrip(repository: PostgresAuditRepository) -> tuple[Fill, Fill]:
    contract = OptionContract(
        instrument_id="synthetic-postgres-outcome-option",
        symbol="TEST",
        option_type=OptionType.CALL,
        strike=Decimal("10"),
        expiration=date(2026, 10, 1),
    )
    fills: list[Fill] = []
    for side, price, when in (
        (OrderSide.BUY_TO_OPEN, Decimal("0.08"), INSTANT),
        (OrderSide.SELL_TO_CLOSE, Decimal("0.12"), INSTANT + timedelta(seconds=30)),
    ):
        order = BrokerOrder(
            created_at=when,
            intent_id=uuid4(),
            environment=repository.binding.environment,
            state=OrderState.FILLED,
            contract=contract,
            side=side,
            quantity=2,
            filled_quantity=2,
            limit_price=price,
            submitted_at=when,
        )
        fill = Fill(
            created_at=when,
            order_id=order.order_id,
            quantity=2,
            price=price,
            market_event_ids=("synthetic-postgres-review-event",),
            fill_model_version="synthetic-fixture-v1",
            deterministic_seed=0,
            reason="Synthetic integration test; no broker interaction",
        )
        await repository.append("orders", order)
        await repository.append("fills", fill)
        fills.append(fill)
    return fills[0], fills[1]


class _RendezvousRepository(PostgresAuditRepository):
    def __init__(self, manager: DatabaseManager, barrier: asyncio.Barrier) -> None:
        super().__init__(manager)
        self._barrier = barrier

    async def append(self, table: str, value: BaseModel | Mapping[str, Any]) -> UUID:
        if table == "trade_outcomes":
            # Both independent pools have completed their empty-outcome reads.
            # The database unique index, not a shared worker lock, decides the winner.
            await self._barrier.wait()
        return await super().append(table, value)


async def test_postgres_outcome_race_deduplicates_across_connection_pools_and_restart(
    postgres_database: PostgresTestDatabase,
) -> None:
    barrier = asyncio.Barrier(2)
    left = _RendezvousRepository(postgres_database.manager(), barrier)
    right = _RendezvousRepository(postgres_database.manager(), barrier)
    entry, exit_fill = await _persist_synthetic_roundtrip(left)
    clock_left = VirtualClock(INSTANT + timedelta(minutes=1))
    clock_right = VirtualClock(INSTANT + timedelta(minutes=2))
    results = await asyncio.wait_for(
        asyncio.gather(
            ClosedPositionReviewWorker(left, clock_left).tick(),
            ClosedPositionReviewWorker(right, clock_right).tick(),
        ),
        timeout=30,
    )
    assert sorted(len(result) for result in results) == [0, 1]
    rows = await left.list_payloads("trade_outcomes")
    assert len(rows) == 1
    original = rows[0]
    payload = original["payload"]
    assert payload["entry_fill_ids"] == [str(entry.fill_id)]
    assert payload["exit_fill_ids"] == [str(exit_fill.fill_id)]
    assert Decimal(payload["gross_entry_cost"]) == Decimal("16")
    assert Decimal(payload["gross_exit_proceeds"]) == Decimal("24")
    assert Decimal(payload["gross_realized_pnl"]) == Decimal("8")
    assert Decimal(payload["gross_return_percent"]) == Decimal("50")
    assert payload["net_realized_pnl"] is payload["fees"] is None
    assert len(payload["source_evidence_hash"]) == 64
    assert await left.healthcheck() and await right.healthcheck()

    fresh = PostgresAuditRepository(postgres_database.manager())
    restarted = ClosedPositionReviewWorker(fresh, VirtualClock(INSTANT + timedelta(hours=1)))
    assert await restarted.tick() == ()
    assert await fresh.list_payloads("trade_outcomes") == [original]
    assert await fresh.list_payloads("fills") == await left.list_payloads("fills")


async def test_postgres_outcome_unique_index_preserves_content_after_conflicting_insert(
    postgres_database: PostgresTestDatabase,
) -> None:
    repository = PostgresAuditRepository(postgres_database.manager())
    await _persist_synthetic_roundtrip(repository)
    worker = ClosedPositionReviewWorker(repository, VirtualClock(INSTANT + timedelta(minutes=1)))
    outcome = (await worker.tick())[0]
    original = await repository.list_payloads("trade_outcomes")
    competing = PostgresAuditRepository(postgres_database.manager())
    with pytest.raises(IntegrityError):
        await competing.append(
            "trade_outcomes", outcome.model_copy(update={"gross_realized_pnl": Decimal("999")})
        )
    assert await competing.healthcheck()
    assert await repository.list_payloads("trade_outcomes") == original
    assert await worker.tick() == ()
