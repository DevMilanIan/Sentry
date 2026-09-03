from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import yaml
from pydantic import Field, ValidationError, model_validator

from app.clock.base import Clock
from app.domain.enums import JudgeDecision
from app.domain.models import DomainModel, JudgeOutput, sha256_json
from app.exceptions import ConfigurationError, DataInvalidError


class DecisionPolicyProfile(DomainModel):
    version: str
    minimum_judge_confidence: Decimal = Field(ge=0, le=1)
    minimum_trade_quality_score: Decimal = Field(ge=0, le=100)
    skeptic_required: bool
    no_unresolved_primary_conflict: bool
    maximum_quote_age_seconds: int = Field(gt=0)
    explicit_capital_opportunity_cost_required: bool = False
    minimum_contract_execution_score: Decimal | None = Field(default=None, ge=0, le=100)
    evidence_complete_required: bool = True


class DecisionPolicySet(DomainModel):
    version: str
    profiles: dict[str, DecisionPolicyProfile]

    @model_validator(mode="after")
    def required_profiles_exist(self) -> DecisionPolicySet:
        required = {"DEMO_EXPLORATORY", "LIVE_CONSERVATIVE"}
        missing = required - set(self.profiles)
        if missing:
            raise ValueError(f"missing decision profiles: {', '.join(sorted(missing))}")
        return self


class PolicyContext(DomainModel):
    trade_quality_score: Decimal = Field(ge=0, le=100)
    quote_observed_at: datetime
    skeptic_completed: bool = False
    unresolved_primary_conflict: bool = False
    evidence_complete: bool = True
    capital_opportunity_cost_addressed: bool = False
    deterministic_rules_passed: bool = True
    contract_execution_score: Decimal | None = Field(default=None, ge=0, le=100)

    @model_validator(mode="after")
    def quote_timestamp_is_aware(self) -> PolicyContext:
        if self.quote_observed_at.tzinfo is None or self.quote_observed_at.utcoffset() is None:
            raise ValueError("quote_observed_at must be timezone-aware")
        object.__setattr__(self, "quote_observed_at", self.quote_observed_at.astimezone(UTC))
        return self


class PolicyOutcome(DomainModel):
    profile_name: str
    policy_version: str
    effective_decision: JudgeDecision
    proceed: bool
    judge_decision: JudgeDecision
    failed_requirements: tuple[str, ...]
    passed_requirements: tuple[str, ...]
    evaluated_at: datetime
    evaluation_hash: str


class DecisionPolicyEvaluator:
    """Apply qualitative thresholds without granting risk or broker authority."""

    def __init__(self, profile_name: str, profile: DecisionPolicyProfile, clock: Clock) -> None:
        self.profile_name = profile_name
        self.profile = profile
        self.clock = clock

    def evaluate(self, judge: JudgeOutput, context: PolicyContext) -> PolicyOutcome:
        now = self.clock.now()
        if now.tzinfo is None or now.utcoffset() is None:
            raise ValueError("Clock must return a timezone-aware timestamp")
        now = now.astimezone(UTC)
        if context.quote_observed_at > now:
            raise DataInvalidError("policy evaluation cannot consume a future quote")
        quote_age = _elapsed_seconds(context.quote_observed_at, now)
        passed: list[str] = []
        failed: list[str] = []

        _record(
            context.deterministic_rules_passed,
            "deterministic_rules_passed",
            passed,
            failed,
        )
        _record(
            judge.decision is JudgeDecision.PASS,
            "judge_pass",
            passed,
            failed,
        )
        _record(
            judge.confidence >= self.profile.minimum_judge_confidence,
            "minimum_judge_confidence",
            passed,
            failed,
        )
        _record(
            context.trade_quality_score >= self.profile.minimum_trade_quality_score,
            "minimum_trade_quality_score",
            passed,
            failed,
        )
        _record(
            quote_age <= Decimal(self.profile.maximum_quote_age_seconds),
            "quote_freshness",
            passed,
            failed,
        )
        if self.profile.skeptic_required:
            _record(context.skeptic_completed, "skeptic_completed", passed, failed)
        if self.profile.no_unresolved_primary_conflict:
            _record(
                not context.unresolved_primary_conflict,
                "no_unresolved_primary_conflict",
                passed,
                failed,
            )
        if self.profile.evidence_complete_required:
            _record(context.evidence_complete, "evidence_complete", passed, failed)
        if self.profile.explicit_capital_opportunity_cost_required:
            _record(
                context.capital_opportunity_cost_addressed,
                "capital_opportunity_cost_addressed",
                passed,
                failed,
            )
        if self.profile.minimum_contract_execution_score is not None:
            _record(
                context.contract_execution_score is not None
                and context.contract_execution_score
                >= self.profile.minimum_contract_execution_score,
                "minimum_contract_execution_score",
                passed,
                failed,
            )

        proceed = not failed
        if proceed:
            effective = JudgeDecision.PASS
        elif not context.deterministic_rules_passed or judge.decision is JudgeDecision.REJECT:
            effective = JudgeDecision.REJECT
        else:
            effective = JudgeDecision.WATCH
        material = {
            "profile_name": self.profile_name,
            "policy_version": self.profile.version,
            "judge": judge.model_dump(mode="json"),
            "context": context.model_dump(mode="json"),
            "evaluated_at": now,
            "failed": failed,
            "passed": passed,
            "effective": effective.value,
        }
        return PolicyOutcome(
            profile_name=self.profile_name,
            policy_version=self.profile.version,
            effective_decision=effective,
            proceed=proceed,
            judge_decision=judge.decision,
            failed_requirements=tuple(failed),
            passed_requirements=tuple(passed),
            evaluated_at=now,
            evaluation_hash=sha256_json(material),
        )


def load_decision_policy_set(
    path: Path = Path("config/decision_policies.yaml"),
) -> DecisionPolicySet:
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
        return DecisionPolicySet.model_validate(raw)
    except (OSError, yaml.YAMLError, ValidationError) as exc:
        raise ConfigurationError(f"invalid decision policy file {path}: {exc}") from exc


def _record(condition: bool, name: str, passed: list[str], failed: list[str]) -> None:
    (passed if condition else failed).append(name)


def _elapsed_seconds(start: datetime, end: datetime) -> Decimal:
    delta = end - start
    return (
        Decimal(delta.days) * Decimal("86400")
        + Decimal(delta.seconds)
        + Decimal(delta.microseconds) / Decimal("1000000")
    )
