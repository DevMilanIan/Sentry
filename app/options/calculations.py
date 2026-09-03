from __future__ import annotations

from collections.abc import Iterable
from datetime import date
from decimal import Decimal, InvalidOperation, localcontext

from pydantic import Field

from app.domain.enums import OptionType
from app.domain.models import DomainModel, OptionContract, OptionQuote

ZERO = Decimal("0")
ONE_HUNDRED = Decimal("100")


class ContractMetrics(DomainModel):
    dte: int
    moneyness_percent: Decimal
    absolute_moneyness_percent: Decimal = Field(ge=0)
    spread_dollars: Decimal = Field(ge=0)
    spread_percent: Decimal | None = Field(default=None, ge=0)
    midpoint: Decimal = Field(ge=0)
    effective_mark: Decimal = Field(ge=0)
    mark_is_sane: bool
    intrinsic_value: Decimal = Field(ge=0)
    extrinsic_value: Decimal = Field(ge=0)
    break_even_at_expiration: Decimal = Field(ge=0)
    ask_contract_cost: Decimal = Field(ge=0)


def percentage_change(previous: Decimal, current: Decimal) -> Decimal:
    if previous == ZERO:
        raise ValueError("percentage change is undefined from zero")
    return (current - previous) / abs(previous) * ONE_HUNDRED


def relative_volume(current_volume: int, baseline_volume: Decimal) -> Decimal:
    if current_volume < 0:
        raise ValueError("current_volume cannot be negative")
    if baseline_volume <= ZERO:
        raise ValueError("baseline_volume must be positive")
    return Decimal(current_volume) / baseline_volume


def moving_average(values: Iterable[Decimal]) -> Decimal:
    materialized = tuple(values)
    if not materialized:
        raise ValueError("moving average requires at least one value")
    return sum(materialized, ZERO) / Decimal(len(materialized))


def realized_volatility(closes: Iterable[Decimal], *, annualization_periods: int = 252) -> Decimal:
    """Sample volatility of log returns using Decimal arithmetic throughout."""

    prices = tuple(closes)
    if len(prices) < 3:
        raise ValueError("realized volatility requires at least three closes")
    if any(price <= ZERO for price in prices):
        raise ValueError("realized volatility requires positive closes")
    if annualization_periods <= 0:
        raise ValueError("annualization_periods must be positive")
    with localcontext() as context:
        context.prec = 34
        try:
            returns = tuple(
                (current / previous).ln()
                for previous, current in zip(prices, prices[1:], strict=False)
            )
            mean = moving_average(returns)
            variance = sum(((value - mean) ** 2 for value in returns), ZERO) / Decimal(
                len(returns) - 1
            )
            return (variance * Decimal(annualization_periods)).sqrt()
        except InvalidOperation as exc:
            raise ValueError("unable to calculate realized volatility") from exc


def days_to_expiration(expiration: date, as_of: date) -> int:
    return (expiration - as_of).days


def midpoint(bid: Decimal, ask: Decimal) -> Decimal:
    _valid_quote_prices(bid, ask)
    return (bid + ask) / Decimal("2")


def quote_spread_dollars(bid: Decimal, ask: Decimal) -> Decimal:
    _valid_quote_prices(bid, ask)
    return ask - bid


def quote_spread_percent(bid: Decimal, ask: Decimal) -> Decimal | None:
    center = midpoint(bid, ask)
    if center == ZERO:
        return None
    return quote_spread_dollars(bid, ask) / center * ONE_HUNDRED


def moneyness_percent(contract: OptionContract, underlying_price: Decimal) -> Decimal:
    """Signed moneyness where positive means out-of-the-money for either option type."""

    if underlying_price <= ZERO:
        raise ValueError("underlying_price must be positive")
    if contract.option_type is OptionType.CALL:
        return (contract.strike - underlying_price) / underlying_price * ONE_HUNDRED
    return (underlying_price - contract.strike) / underlying_price * ONE_HUNDRED


def intrinsic_value(contract: OptionContract, underlying_price: Decimal) -> Decimal:
    if underlying_price < ZERO:
        raise ValueError("underlying_price cannot be negative")
    if contract.option_type is OptionType.CALL:
        return max(underlying_price - contract.strike, ZERO)
    return max(contract.strike - underlying_price, ZERO)


def extrinsic_value(
    contract: OptionContract, underlying_price: Decimal, premium: Decimal
) -> Decimal:
    if premium < ZERO:
        raise ValueError("premium cannot be negative")
    return max(premium - intrinsic_value(contract, underlying_price), ZERO)


def break_even_at_expiration(contract: OptionContract, premium: Decimal) -> Decimal:
    if premium < ZERO:
        raise ValueError("premium cannot be negative")
    if contract.option_type is OptionType.CALL:
        return contract.strike + premium
    return max(contract.strike - premium, ZERO)


def contract_cost(premium: Decimal, *, quantity: int = 1, multiplier: int = 100) -> Decimal:
    if premium < ZERO:
        raise ValueError("premium cannot be negative")
    if quantity <= 0 or multiplier <= 0:
        raise ValueError("quantity and multiplier must be positive")
    return premium * Decimal(quantity) * Decimal(multiplier)


def contract_metrics(quote: OptionQuote, underlying_price: Decimal, as_of: date) -> ContractMetrics:
    center = midpoint(quote.bid, quote.ask)
    effective_mark = quote.mark if quote.mark is not None else center
    mark_is_sane = (
        quote.bid <= effective_mark <= quote.ask if quote.ask else effective_mark == quote.bid
    )
    premium_for_value = effective_mark if mark_is_sane else center
    signed_moneyness = moneyness_percent(quote.contract, underlying_price)
    return ContractMetrics(
        dte=days_to_expiration(quote.contract.expiration, as_of),
        moneyness_percent=signed_moneyness,
        absolute_moneyness_percent=abs(signed_moneyness),
        spread_dollars=quote_spread_dollars(quote.bid, quote.ask),
        spread_percent=quote_spread_percent(quote.bid, quote.ask),
        midpoint=center,
        effective_mark=effective_mark,
        mark_is_sane=mark_is_sane,
        intrinsic_value=intrinsic_value(quote.contract, underlying_price),
        extrinsic_value=extrinsic_value(quote.contract, underlying_price, premium_for_value),
        break_even_at_expiration=break_even_at_expiration(quote.contract, quote.ask),
        ask_contract_cost=contract_cost(
            quote.ask,
            multiplier=quote.contract.multiplier,
        ),
    )


def _valid_quote_prices(bid: Decimal, ask: Decimal) -> None:
    if bid < ZERO or ask < ZERO:
        raise ValueError("quote prices cannot be negative")
    if bid > ask:
        raise ValueError("bid cannot exceed ask")
