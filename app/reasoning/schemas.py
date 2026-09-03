from __future__ import annotations

from decimal import Decimal

from pydantic import Field, model_validator

from app.domain.enums import Direction, JudgeDecision
from app.domain.models import DomainModel, JudgeOutput


class SituationAnalysis(DomainModel):
    materiality: Decimal = Field(ge=0, le=1)
    directional_bias: Direction
    time_horizon: str = Field(min_length=1, max_length=256)
    primary_driver: str = Field(min_length=1, max_length=2_000)
    supporting_facts: tuple[str, ...]
    uncertainties: tuple[str, ...]
    thesis_invalidation_conditions: tuple[str, ...]
    research_needed: tuple[str, ...]
    # Required-but-nullable so every output explicitly states whether it is
    # abstaining; omission is not equivalent to a considered null decision.
    abstain_reason: str | None = Field(max_length=2_000)
    inference_notes: tuple[str, ...] = ()

    @model_validator(mode="after")
    def abstention_is_explicit(self) -> SituationAnalysis:
        if self.directional_bias is Direction.NONE and not self.abstain_reason:
            raise ValueError("a non-directional situation requires an abstain_reason")
        return self


class BullAnalysis(DomainModel):
    thesis: str = Field(min_length=1, max_length=4_000)
    supporting_fact_ids: tuple[str, ...]
    upside_drivers: tuple[str, ...]
    required_evidence: tuple[str, ...]
    failure_conditions: tuple[str, ...]
    confidence: Decimal = Field(ge=0, le=1)
    inference_notes: tuple[str, ...] = ()


class BearAnalysis(DomainModel):
    downside_thesis: str = Field(min_length=1, max_length=4_000)
    supporting_fact_ids: tuple[str, ...]
    downside_drivers: tuple[str, ...]
    bullish_option_failure_modes: tuple[str, ...]
    required_evidence: tuple[str, ...]
    confidence: Decimal = Field(ge=0, le=1)
    inference_notes: tuple[str, ...] = ()


class SkepticAnalysis(DomainModel):
    hidden_assumptions: tuple[str, ...]
    stale_or_circular_evidence: tuple[str, ...]
    catalyst_priced_in_arguments: tuple[str, ...]
    timing_challenges: tuple[str, ...]
    contract_economics_challenges: tuple[str, ...]
    thesis_right_but_option_loses: tuple[str, ...]
    decision_changing_data: tuple[str, ...]
    referenced_fact_ids: tuple[str, ...]
    unresolved_primary_source_conflict: bool
    inference_notes: tuple[str, ...] = ()


class JudgeAnalysis(DomainModel):
    decision: JudgeDecision
    directional_thesis: Direction
    # Required-but-nullable: the model must explicitly choose a frozen rank or
    # explicitly return null.  Omitting this safety-significant field is invalid.
    selected_candidate_rank: int | None = Field(ge=1)
    confidence: Decimal = Field(ge=0, le=1)
    expected_time_window: str = Field(min_length=1, max_length=256)
    catalyst_strength: Decimal = Field(ge=0, le=1)
    contract_quality_critique: str = Field(min_length=1, max_length=4_000)
    thesis: str = Field(min_length=1, max_length=4_000)
    invalidation_conditions: tuple[str, ...]
    recheck_conditions: tuple[str, ...]
    reasons_to_abstain: tuple[str, ...]
    rationale: str = Field(min_length=1, max_length=4_000)
    referenced_fact_ids: tuple[str, ...]
    inference_notes: tuple[str, ...] = ()
    evidence_complete: bool
    capital_opportunity_cost_addressed: bool = False

    @model_validator(mode="after")
    def selected_rank_only_on_pass(self) -> JudgeAnalysis:
        if self.decision is JudgeDecision.PASS and self.selected_candidate_rank is None:
            raise ValueError("PASS requires a selected candidate rank")
        if self.decision is not JudgeDecision.PASS and self.selected_candidate_rank is not None:
            raise ValueError("non-PASS output cannot select a candidate")
        return self

    def to_domain(self) -> JudgeOutput:
        return JudgeOutput(
            decision=self.decision,
            directional_thesis=self.directional_thesis,
            selected_candidate_rank=self.selected_candidate_rank,
            confidence=self.confidence,
            expected_time_window=self.expected_time_window,
            catalyst_strength=self.catalyst_strength,
            contract_quality_critique=self.contract_quality_critique,
            thesis=self.thesis,
            invalidation_conditions=self.invalidation_conditions,
            recheck_conditions=self.recheck_conditions,
            reasons_to_abstain=self.reasons_to_abstain,
            rationale=self.rationale,
        )


# Explicit schema aliases keep role-facing imports readable.
SituationSchema = SituationAnalysis
BullSchema = BullAnalysis
BearSchema = BearAnalysis
SkepticSchema = SkepticAnalysis
JudgeSchema = JudgeAnalysis
