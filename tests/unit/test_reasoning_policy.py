from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

from app.clock.base import VirtualClock
from app.domain.enums import Direction, JudgeDecision
from app.domain.models import JudgeOutput
from app.reasoning.policies import (
    DecisionPolicyEvaluator,
    PolicyContext,
    load_decision_policy_set,
)


def _judge(confidence: str = "0.70") -> JudgeOutput:
    return JudgeOutput(
        decision=JudgeDecision.PASS,
        directional_thesis=Direction.BULLISH,
        selected_candidate_rank=1,
        confidence=Decimal(confidence),
        expected_time_window="ten sessions",
        catalyst_strength=Decimal("0.8"),
        contract_quality_critique="acceptable but not exceptional",
        thesis="verified catalyst may drive repricing",
        invalidation_conditions=("catalyst withdrawn",),
        recheck_conditions=("spread widens",),
        reasons_to_abstain=(),
        rationale="evidence and contract align",
    )


def test_demo_and_live_policy_diverge_for_same_frozen_evidence() -> None:
    now = datetime(2026, 1, 5, 15, 0, tzinfo=UTC)
    clock = VirtualClock(now)
    policies = load_decision_policy_set()
    context = PolicyContext(
        trade_quality_score=Decimal("70"),
        quote_observed_at=now - timedelta(seconds=20),
        skeptic_completed=False,
        evidence_complete=True,
        capital_opportunity_cost_addressed=False,
    )
    demo = DecisionPolicyEvaluator(
        "DEMO_EXPLORATORY", policies.profiles["DEMO_EXPLORATORY"], clock
    ).evaluate(_judge(), context)
    live = DecisionPolicyEvaluator(
        "LIVE_CONSERVATIVE", policies.profiles["LIVE_CONSERVATIVE"], clock
    ).evaluate(_judge(), context)
    assert demo.proceed
    assert demo.effective_decision is JudgeDecision.PASS
    assert not live.proceed
    assert live.effective_decision is JudgeDecision.WATCH
    assert "skeptic_completed" in live.failed_requirements


def test_deterministic_failure_cannot_be_overridden_by_confidence() -> None:
    now = datetime(2026, 1, 5, 15, 0, tzinfo=UTC)
    policies = load_decision_policy_set()
    outcome = DecisionPolicyEvaluator(
        "DEMO_EXPLORATORY",
        policies.profiles["DEMO_EXPLORATORY"],
        VirtualClock(now),
    ).evaluate(
        _judge("1"),
        PolicyContext(
            trade_quality_score=100,
            quote_observed_at=now,
            deterministic_rules_passed=False,
        ),
    )
    assert not outcome.proceed
    assert outcome.effective_decision is JudgeDecision.REJECT
