from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from uuid import uuid4

import pytest

from app.clock.base import VirtualClock
from app.clock.market_calendar import CalendarCoverageError
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


@pytest.mark.parametrize(
    "at,expected_eod",
    [
        (datetime(2026, 11, 27, 17, 44, 59, tzinfo=UTC), False),
        (datetime(2026, 11, 27, 17, 45, tzinfo=UTC), True),
        (datetime(2026, 9, 3, 19, 44, 59, tzinfo=UTC), False),
        (datetime(2026, 9, 3, 19, 45, tzinfo=UTC), True),
    ],
)
def test_end_of_day_exit_keeps_fifteen_minute_buffer_before_early_close(
    at: datetime, expected_eod: bool
) -> None:
    manager = PositionManager(VirtualClock(at), ExitPolicy(version="eod-v1", end_of_day_exit=True))
    contract = CONTRACT.model_copy(update={"expiration": date(2026, 12, 18)})
    holding = position().model_copy(update={"contract": contract})
    current_quote = quote("0.07", "0.08", at=at).model_copy(update={"contract": contract})
    decision = manager.evaluate_exit(holding, current_quote)
    assert (ExitTrigger.END_OF_DAY in decision.triggers) is expected_eod
    assert decision.executable is expected_eod


def test_end_of_day_exit_requires_verified_session_calendar() -> None:
    at = datetime(2029, 1, 2, 21, tzinfo=UTC)
    manager = PositionManager(VirtualClock(at), ExitPolicy(version="eod-v1", end_of_day_exit=True))
    with pytest.raises(CalendarCoverageError, match="unverified"):
        manager.evaluate_exit(position(), quote("0.07", "0.08", at=at))
