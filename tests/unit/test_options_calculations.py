from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

from app.domain.enums import OptionType
from app.domain.models import OptionContract
from app.options.calculations import (
    break_even_at_expiration,
    contract_cost,
    days_to_expiration,
    extrinsic_value,
    intrinsic_value,
    midpoint,
    moneyness_percent,
    moving_average,
    percentage_change,
    quote_spread_percent,
    realized_volatility,
    relative_volume,
)


def _contract(option_type: OptionType, strike: str = "102") -> OptionContract:
    return OptionContract(
        instrument_id=f"test-{option_type.value}",
        symbol="TEST",
        option_type=option_type,
        strike=Decimal(strike),
        expiration=date(2026, 1, 23),
    )


def test_decimal_market_calculations_are_exact() -> None:
    assert percentage_change(Decimal("80"), Decimal("100")) == Decimal("25")
    assert relative_volume(150, Decimal("100")) == Decimal("1.5")
    assert moving_average((Decimal("1"), Decimal("2"), Decimal("3"))) == 2
    assert midpoint(Decimal("0.10"), Decimal("0.20")) == Decimal("0.15")
    assert quote_spread_percent(Decimal("0.10"), Decimal("0.20")) == Decimal(
        "66.66666666666666666666666667"
    )
    assert contract_cost(Decimal("0.20")) == Decimal("20.00")
    assert days_to_expiration(date(2026, 1, 23), date(2026, 1, 5)) == 18


def test_option_value_and_moneyness_are_direction_aware() -> None:
    call = _contract(OptionType.CALL)
    put = _contract(OptionType.PUT, "98")
    assert moneyness_percent(call, Decimal("100")) == Decimal("2.00")
    assert moneyness_percent(put, Decimal("100")) == Decimal("2.00")
    assert intrinsic_value(call, Decimal("105")) == Decimal("3")
    assert extrinsic_value(call, Decimal("105"), Decimal("4")) == Decimal("1")
    assert break_even_at_expiration(call, Decimal("0.20")) == Decimal("102.20")
    assert break_even_at_expiration(put, Decimal("0.20")) == Decimal("97.80")


def test_realized_volatility_is_decimal_and_validated() -> None:
    volatility = realized_volatility(
        (Decimal("100"), Decimal("101"), Decimal("99"), Decimal("102"))
    )
    assert isinstance(volatility, Decimal)
    assert volatility > 0
    with pytest.raises(ValueError):
        realized_volatility((Decimal("100"), Decimal("101")))
