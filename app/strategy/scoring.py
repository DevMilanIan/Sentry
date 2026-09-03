from __future__ import annotations

from collections.abc import Mapping
from decimal import ROUND_HALF_UP, Decimal

from pydantic import Field, field_validator, model_validator

from app.domain.models import DomainModel

HUNDRED = Decimal("100")
SCORE_QUANTUM = Decimal("0.01")

SURVEILLANCE_WEIGHTS: dict[str, Decimal] = {
    "catalyst_priority": Decimal("20"),
    "momentum_anomaly": Decimal("25"),
    "technical_structure": Decimal("15"),
    "market_sector_alignment": Decimal("10"),
    "underlying_liquidity": Decimal("10"),
    "federal_exposure": Decimal("10"),
    "event_urgency": Decimal("10"),
}

TRADE_QUALITY_WEIGHTS: dict[str, Decimal] = {
    "catalyst_materiality": Decimal("20"),
    "adversarial_evidence": Decimal("20"),
    "contract_execution": Decimal("20"),
    "timing_confirmation": Decimal("15"),
    "payoff_plausibility": Decimal("15"),
    "market_sector_alignment": Decimal("10"),
}

TRADE_QUALITY_PENALTIES: dict[str, Decimal] = {
    "extreme_spread": Decimal("15"),
    "very_low_delta": Decimal("8"),
    "excessive_short_dte": Decimal("10"),
    "catalyst_priced_in": Decimal("12"),
    "unmodeled_event_risk": Decimal("15"),
    "stale_data": Decimal("20"),
    "conflicting_primary_sources": Decimal("20"),
    "low_underlying_liquidity": Decimal("15"),
    "high_iv_vs_expected_move": Decimal("12"),
}


class ScoringWeights(DomainModel):
    version: str
    weights: dict[str, Decimal]

    @field_validator("weights")
    @classmethod
    def positive_weights(cls, value: dict[str, Decimal]) -> dict[str, Decimal]:
        if not value:
            raise ValueError("scoring weights cannot be empty")
        if any(weight <= 0 for weight in value.values()):
            raise ValueError("scoring weights must be positive")
        return value


class ScoreComponent(DomainModel):
    name: str
    raw_value: Decimal = Field(ge=0, le=100)
    weight: Decimal = Field(gt=0)
    weighted_contribution: Decimal = Field(ge=0, le=100)
    supplied: bool


class ScoreBreakdown(DomainModel):
    version: str
    base_score: Decimal = Field(ge=0, le=100)
    penalty_total: Decimal = Field(ge=0)
    final_score: Decimal = Field(ge=0, le=100)
    components: tuple[ScoreComponent, ...]
    penalties: dict[str, Decimal] = Field(default_factory=dict)
    missing_components: tuple[str, ...] = ()

    @model_validator(mode="after")
    def score_math_is_consistent(self) -> ScoreBreakdown:
        expected = max(Decimal("0"), self.base_score - self.penalty_total).quantize(
            SCORE_QUANTUM, rounding=ROUND_HALF_UP
        )
        if self.final_score != expected:
            raise ValueError("final score does not equal base less penalties")
        return self


class TransparentScorer:
    def __init__(self, weights: ScoringWeights) -> None:
        self.weights = weights

    def score(
        self,
        values: Mapping[str, Decimal | int | str],
        *,
        penalties: Mapping[str, Decimal | int | str] | None = None,
    ) -> ScoreBreakdown:
        unknown = set(values) - set(self.weights.weights)
        if unknown:
            raise ValueError(f"unknown scoring components: {', '.join(sorted(unknown))}")
        total_weight = sum(self.weights.weights.values(), Decimal("0"))
        components: list[ScoreComponent] = []
        missing: list[str] = []
        contribution_total = Decimal("0")
        for name, weight in self.weights.weights.items():
            supplied = name in values
            raw = Decimal(str(values.get(name, 0)))
            if not Decimal("0") <= raw <= HUNDRED:
                raise ValueError(f"component {name} must be in [0, 100]")
            if not supplied:
                missing.append(name)
            contribution = raw * weight / total_weight
            contribution_total += contribution
            components.append(
                ScoreComponent(
                    name=name,
                    raw_value=raw,
                    weight=weight,
                    weighted_contribution=contribution.quantize(
                        SCORE_QUANTUM, rounding=ROUND_HALF_UP
                    ),
                    supplied=supplied,
                )
            )
        normalized_penalties: dict[str, Decimal] = {}
        for name, raw_value in (penalties or {}).items():
            value = Decimal(str(raw_value))
            if value < 0:
                raise ValueError(f"penalty {name} cannot be negative")
            normalized_penalties[name] = value
        base = min(HUNDRED, max(Decimal("0"), contribution_total)).quantize(
            SCORE_QUANTUM, rounding=ROUND_HALF_UP
        )
        penalty_total = sum(normalized_penalties.values(), Decimal("0"))
        final = max(Decimal("0"), base - penalty_total).quantize(
            SCORE_QUANTUM, rounding=ROUND_HALF_UP
        )
        return ScoreBreakdown(
            version=self.weights.version,
            base_score=base,
            penalty_total=penalty_total,
            final_score=final,
            components=tuple(components),
            penalties=normalized_penalties,
            missing_components=tuple(missing),
        )


class SurveillanceScorer(TransparentScorer):
    def __init__(
        self,
        *,
        version: str = "surveillance-v1",
        weights: Mapping[str, Decimal] | None = None,
    ) -> None:
        super().__init__(
            ScoringWeights(
                version=version,
                weights=dict(weights or SURVEILLANCE_WEIGHTS),
            )
        )


class TradeQualityScorer(TransparentScorer):
    def __init__(
        self,
        *,
        version: str = "trade-quality-v1",
        weights: Mapping[str, Decimal] | None = None,
        penalty_points: Mapping[str, Decimal] | None = None,
    ) -> None:
        super().__init__(
            ScoringWeights(
                version=version,
                weights=dict(weights or TRADE_QUALITY_WEIGHTS),
            )
        )
        self.penalty_points = dict(penalty_points or TRADE_QUALITY_PENALTIES)

    def score_with_flags(
        self,
        values: Mapping[str, Decimal | int | str],
        *,
        penalty_flags: Mapping[str, bool],
    ) -> ScoreBreakdown:
        unknown = set(penalty_flags) - set(self.penalty_points)
        if unknown:
            raise ValueError(f"unknown trade-quality penalties: {', '.join(sorted(unknown))}")
        penalties = {
            name: self.penalty_points[name] for name, active in penalty_flags.items() if active
        }
        return self.score(values, penalties=penalties)
