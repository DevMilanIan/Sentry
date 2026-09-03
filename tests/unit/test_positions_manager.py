from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from uuid import uuid4

from app.clock.base import VirtualClock
from app.domain.enums import ExecutionEnvironment, OptionType
from app.domain.models import OptionContract, OptionQuote, Position, ProviderMetadata
from app.positions import ExitPolicy, ExitTrigger, PositionManager

NOW = datetime(2026, 9, 2, 15, 0, tzinfo=UTC)
CONTRACT = OptionContract(
    instrument_id="opt-1",
    symbol="TEST",
    option_type=OptionType.PUT,
    strike=Decimal("10"),
    expiration=date(2026, 9, 18),
)


def position() -> Position:
    return Position(
        created_at=NOW,
        environment=ExecutionEnvironment.DEMO,
        contract=CONTRACT,
        quantity=1,
        average_entry_price=Decimal("0.08"),
        current_bid=Decimal("0.07"),
        current_ask=Decimal("0.08"),
        thesis_id=uuid4(),
        invalidation_conditions=("underlying above 11",),
        exit_policy_version="exit-v1",
    )


def quote(bid: str, ask: str, *, at: datetime = NOW) -> OptionQuote:
    return OptionQuote(
        contract=CONTRACT,
        bid=Decimal(bid),
        ask=Decimal(ask),
        volume=100,
        open_interest=100,
        metadata=ProviderMetadata(
            provider="fixture",
            capability_version="v1",
            observed_at=at,
            effective_at=at,
        ),
    )


def test_profit_and_thesis_triggers_are_deterministic() -> None:
    manager = PositionManager(
        VirtualClock(NOW), ExitPolicy(version="exit-v1", market_timezone="UTC")
    )

    decision = manager.evaluate_exit(
        position(),
        quote("0.12", "0.13"),
        invalidated_conditions=("underlying above 11",),
    )

    assert decision.should_exit and decision.executable
    assert decision.limit_price == Decimal("0.12")
    assert ExitTrigger.PROFIT_TARGET in decision.triggers
    assert ExitTrigger.THESIS_INVALIDATED in decision.triggers
    assert decision.unrealized_pnl == Decimal("4.00")


def test_stale_or_zero_bid_never_invents_an_executable_exit_price() -> None:
    manager = PositionManager(
        VirtualClock(NOW), ExitPolicy(version="exit-v1", market_timezone="UTC")
    )

    decision = manager.evaluate_exit(
        position(),
        quote("0", "0.08", at=NOW - timedelta(minutes=5)),
        hard_portfolio_emergency=True,
    )

    assert decision.should_exit
    assert not decision.executable
    assert decision.limit_price is None
    assert ExitTrigger.HARD_PORTFOLIO_EMERGENCY in decision.triggers
    assert ExitTrigger.LIQUIDITY_DETERIORATION in decision.triggers
