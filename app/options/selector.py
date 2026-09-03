from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime
from decimal import ROUND_HALF_UP, Decimal
from typing import Protocol
from zoneinfo import ZoneInfo

from pydantic import Field, model_validator

from app.clock.base import Clock
from app.domain.enums import Direction, OptionType, SelectorStatus
from app.domain.models import ContractSelection, DomainModel, OptionQuote
from app.options.calculations import ContractMetrics, contract_metrics


class _RiskLimits(Protocol):
    @property
    def max_new_trade_premium_dollars(self) -> Decimal: ...


class _LiquidityLimits(Protocol):
    @property
    def require_nonzero_bid(self) -> bool: ...

    @property
    def starting_minimum_open_interest(self) -> int: ...

    @property
    def starting_minimum_option_volume(self) -> int: ...

    @property
    def starting_maximum_bid_ask_percent(self) -> Decimal: ...


class RiskConfigLike(Protocol):
    @property
    def risk(self) -> _RiskLimits: ...

    @property
    def liquidity(self) -> _LiquidityLimits: ...


SCORE_QUANTUM = Decimal("0.01")
EASTERN = ZoneInfo("America/New_York")


class ContractSelectorConfig(DomainModel):
    version: str = "selector-v1"
    minimum_dte: int = Field(default=7, ge=0)
    maximum_dte: int = Field(default=45, ge=0)
    maximum_absolute_moneyness_percent: Decimal = Field(default=Decimal("20"), ge=0)
    maximum_candidates: int = Field(default=3, ge=1, le=20)
    maximum_contract_cost: Decimal = Field(gt=0)
    require_nonzero_bid: bool = True
    minimum_open_interest: int = Field(default=0, ge=0)
    minimum_option_volume: int = Field(default=0, ge=0)
    maximum_bid_ask_percent: Decimal = Field(default=Decimal("25"), gt=0)
    maximum_implied_volatility: Decimal | None = Field(default=None, gt=0)
    target_dte: int = Field(default=21, ge=0)
    target_absolute_moneyness_percent: Decimal = Field(default=Decimal("3"), ge=0)

    @model_validator(mode="after")
    def ranges_are_ordered(self) -> ContractSelectorConfig:
        if self.minimum_dte > self.maximum_dte:
            raise ValueError("minimum_dte exceeds maximum_dte")
        if not self.minimum_dte <= self.target_dte <= self.maximum_dte:
            raise ValueError("target_dte must fall inside the configured DTE range")
        return self

    @classmethod
    def from_risk_config(
        cls,
        risk: RiskConfigLike,
        *,
        minimum_dte: int = 7,
        maximum_dte: int = 45,
        maximum_absolute_moneyness_percent: Decimal = Decimal("20"),
        maximum_candidates: int = 3,
        version: str = "selector-v1",
    ) -> ContractSelectorConfig:
        return cls(
            version=version,
            minimum_dte=minimum_dte,
            maximum_dte=maximum_dte,
            maximum_absolute_moneyness_percent=maximum_absolute_moneyness_percent,
            maximum_candidates=maximum_candidates,
            maximum_contract_cost=risk.risk.max_new_trade_premium_dollars,
            require_nonzero_bid=risk.liquidity.require_nonzero_bid,
            minimum_open_interest=risk.liquidity.starting_minimum_open_interest,
            minimum_option_volume=risk.liquidity.starting_minimum_option_volume,
            maximum_bid_ask_percent=risk.liquidity.starting_maximum_bid_ask_percent,
        )


class ContractEvaluation(DomainModel):
    quote: OptionQuote
    metrics: ContractMetrics
    eligible: bool
    quality_score: Decimal = Field(ge=0, le=100)
    rejection_reasons: tuple[str, ...] = ()
    penalties: dict[str, Decimal] = Field(default_factory=dict)


class DetailedContractSelection(DomainModel):
    selection: ContractSelection
    evaluations: tuple[ContractEvaluation, ...]
    selector_version: str
    evaluated_at: datetime


class ContractSelector:
    """Fail-closed filters followed by a deterministic, explainable ranking."""

    def __init__(self, config: ContractSelectorConfig, clock: Clock) -> None:
        self.config = config
        self.clock = clock

    def select(
        self,
        direction: Direction,
        underlying_price: Decimal,
        quotes: Sequence[OptionQuote],
        *,
        maximum_contract_cost: Decimal | None = None,
    ) -> ContractSelection:
        return self.evaluate(
            direction,
            underlying_price,
            quotes,
            maximum_contract_cost=maximum_contract_cost,
        ).selection

    def evaluate(
        self,
        direction: Direction,
        underlying_price: Decimal,
        quotes: Sequence[OptionQuote],
        *,
        maximum_contract_cost: Decimal | None = None,
    ) -> DetailedContractSelection:
        if underlying_price <= 0:
            raise ValueError("underlying_price must be positive")
        evaluated_at = self.clock.now()
        if evaluated_at.tzinfo is None or evaluated_at.utcoffset() is None:
            raise ValueError("Clock must return a timezone-aware timestamp")
        option_type = _option_type(direction)
        cap = maximum_contract_cost or self.config.maximum_contract_cost
        if cap <= 0:
            raise ValueError("maximum contract cost must be positive")
        as_of_date = evaluated_at.astimezone(EASTERN).date()
        evaluations: list[ContractEvaluation] = []
        for quote in quotes:
            metrics = contract_metrics(quote, underlying_price, as_of_date)
            reasons: list[str] = []
            penalties: dict[str, Decimal] = {}
            metadata = quote.metadata
            if metadata.observed_at > evaluated_at or metadata.effective_at > evaluated_at:
                reasons.append("quote_not_yet_available")
            if quote.contract.option_type is not option_type:
                reasons.append("direction_mismatch")
            if metrics.dte < self.config.minimum_dte:
                reasons.append("dte_below_minimum")
            if metrics.dte > self.config.maximum_dte:
                reasons.append("dte_above_maximum")
            if quote.ask <= 0:
                reasons.append("no_executable_ask")
            if metrics.ask_contract_cost > cap:
                reasons.append("premium_cap_exceeded")
            if self.config.require_nonzero_bid and quote.bid <= 0:
                reasons.append("zero_bid")
            if metrics.spread_percent is None:
                reasons.append("undefined_spread_percent")
            elif metrics.spread_percent > self.config.maximum_bid_ask_percent:
                reasons.append("spread_too_wide")
            if (
                quote.open_interest is not None
                and quote.open_interest < self.config.minimum_open_interest
            ):
                reasons.append("open_interest_below_minimum")
            if quote.volume is not None and quote.volume < self.config.minimum_option_volume:
                reasons.append("volume_below_minimum")
            if metrics.absolute_moneyness_percent > self.config.maximum_absolute_moneyness_percent:
                reasons.append("moneyness_outside_limit")
            if (
                self.config.maximum_implied_volatility is not None
                and quote.implied_volatility is not None
                and quote.implied_volatility > self.config.maximum_implied_volatility
            ):
                reasons.append("implied_volatility_above_limit")
            if not metrics.mark_is_sane:
                penalties["mark_outside_market"] = Decimal("8")
            if quote.open_interest is None:
                penalties["open_interest_missing"] = Decimal("4")
            if quote.volume is None:
                penalties["volume_missing"] = Decimal("3")
            quality = _quality_score(quote, metrics, self.config, penalties)
            evaluations.append(
                ContractEvaluation(
                    quote=quote,
                    metrics=metrics,
                    eligible=not reasons,
                    quality_score=quality,
                    rejection_reasons=tuple(reasons),
                    penalties=penalties,
                )
            )

        eligible = [evaluation for evaluation in evaluations if evaluation.eligible]
        eligible.sort(key=_ranking_key)
        ranked = tuple(item.quote for item in eligible[: self.config.maximum_candidates])
        rejected = {
            item.quote.contract.instrument_id: item.rejection_reasons
            for item in evaluations
            if item.rejection_reasons
        }
        status = SelectorStatus.CONTRACT_FOUND if ranked else SelectorStatus.NO_CONTRACT
        selection = ContractSelection(
            status=status,
            ranked_quotes=ranked,
            rejected_reasons=rejected,
        )
        evaluations.sort(key=lambda item: item.quote.contract.instrument_id)
        return DetailedContractSelection(
            selection=selection,
            evaluations=tuple(evaluations),
            selector_version=self.config.version,
            evaluated_at=evaluated_at.astimezone(UTC),
        )


def _option_type(direction: Direction) -> OptionType:
    if direction is Direction.BULLISH:
        return OptionType.CALL
    if direction is Direction.BEARISH:
        return OptionType.PUT
    raise ValueError("contract selection requires a bullish or bearish direction")


def _quality_score(
    quote: OptionQuote,
    metrics: ContractMetrics,
    config: ContractSelectorConfig,
    penalties: dict[str, Decimal],
) -> Decimal:
    spread = metrics.spread_percent or Decimal("100")
    spread_score = max(Decimal("0"), Decimal("100") - spread * Decimal("3"))
    dte_distance = abs(Decimal(metrics.dte - config.target_dte))
    dte_span = Decimal(max(config.maximum_dte - config.minimum_dte, 1))
    dte_score = max(Decimal("0"), Decimal("100") - dte_distance / dte_span * 100)
    money_distance = abs(
        metrics.absolute_moneyness_percent - config.target_absolute_moneyness_percent
    )
    money_span = max(config.maximum_absolute_moneyness_percent, Decimal("1"))
    money_score = max(Decimal("0"), Decimal("100") - money_distance / money_span * 100)
    interest_score = _liquidity_score(quote.open_interest, config.minimum_open_interest)
    volume_score = _liquidity_score(quote.volume, config.minimum_option_volume)
    delta_score = Decimal("50")
    if quote.delta is not None:
        delta_distance = abs(abs(quote.delta) - Decimal("0.45"))
        delta_score = max(Decimal("0"), Decimal("100") - delta_distance * Decimal("200"))
    score = (
        spread_score * Decimal("0.35")
        + dte_score * Decimal("0.20")
        + money_score * Decimal("0.20")
        + interest_score * Decimal("0.10")
        + volume_score * Decimal("0.05")
        + delta_score * Decimal("0.10")
        - sum(penalties.values(), Decimal("0"))
    )
    return min(Decimal("100"), max(Decimal("0"), score)).quantize(
        SCORE_QUANTUM, rounding=ROUND_HALF_UP
    )


def _liquidity_score(value: int | None, minimum: int) -> Decimal:
    if value is None:
        return Decimal("40")
    if minimum == 0:
        return Decimal("100") if value > 0 else Decimal("60")
    ratio = Decimal(value) / Decimal(minimum)
    return min(Decimal("100"), ratio * Decimal("70"))


def _ranking_key(evaluation: ContractEvaluation) -> tuple[Decimal, Decimal, int, str]:
    spread = evaluation.metrics.spread_percent or Decimal("999999")
    open_interest = evaluation.quote.open_interest or 0
    return (
        -evaluation.quality_score,
        spread,
        -open_interest,
        evaluation.quote.contract.instrument_id,
    )
