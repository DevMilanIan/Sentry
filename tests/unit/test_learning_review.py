from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone
from decimal import Decimal

import pytest
from pydantic import ValidationError

from app.learning.review import ConfigurationChangeProposal, LearningReviewer


def _proposal_arguments(**overrides: object) -> dict[str, object]:
    values: dict[str, object] = {
        "created_at": datetime(2026, 9, 3, 15, 0, tzinfo=UTC),
        "sample_size": 30,
        "affected_keys": ("risk.max_new_trade_premium_dollars",),
        "proposed_values": {"risk.max_new_trade_premium_dollars": Decimal("35")},
        "before": {"rejected_candidates": Decimal("12")},
        "after": {"rejected_candidates": Decimal("9")},
        "benefit": "fewer false rejections",
        "downside": "slightly higher premium at risk",
        "confidence": Decimal("0.75"),
    }
    values.update(overrides)
    return values


def _propose(**overrides: object) -> ConfigurationChangeProposal:
    return LearningReviewer().propose(**_proposal_arguments(**overrides))  # type: ignore[arg-type]


def test_minimum_sample_size_is_inclusive_and_proposal_is_auditable() -> None:
    proposal = _propose(sample_size=LearningReviewer.minimum_sample_size)

    assert proposal.evidence_sample_size == 30
    assert proposal.affected_keys == ("risk.max_new_trade_premium_dollars",)
    assert proposal.proposed_values == {
        "risk.max_new_trade_premium_dollars": Decimal("35")
    }
    assert proposal.before_replay_metrics == {"rejected_candidates": Decimal("12")}
    assert proposal.after_replay_metrics == {"rejected_candidates": Decimal("9")}
    assert proposal.expected_benefit == "fewer false rejections"
    assert proposal.possible_downside == "slightly higher premium at risk"
    assert proposal.confidence == Decimal("0.75")
    assert proposal.applied is False


@pytest.mark.parametrize("sample_size", [0, 1, 29])
def test_reviewer_rejects_samples_below_the_learning_threshold(sample_size: int) -> None:
    with pytest.raises(ValueError, match="at least 30 observations are required"):
        _propose(sample_size=sample_size)


@pytest.mark.parametrize(
    "affected_keys",
    [
        ("risk.prohibited",),
        ("risk.prohibited.naked_options",),
        ("risk.max_open_positions", "risk.prohibited.borrowed_margin"),
    ],
)
def test_reviewer_cannot_propose_changes_to_v1_prohibitions(
    affected_keys: tuple[str, ...],
) -> None:
    with pytest.raises(ValueError, match="cannot propose relaxing V1 prohibitions"):
        _propose(affected_keys=affected_keys)


@pytest.mark.parametrize(
    "prohibited_key",
    [
        "prohibited.naked_options",
        "Prohibited.short_options",
        "configuration.prohibited.short_options",
        "configuration.risk.prohibited.borrowed_margin",
    ],
)
def test_reviewer_rejects_every_semantic_path_to_a_prohibition(
    prohibited_key: str,
) -> None:
    with pytest.raises(ValueError, match="cannot propose relaxing V1 prohibitions"):
        _propose(
            affected_keys=(prohibited_key,),
            proposed_values={prohibited_key: False},
        )


def test_reviewer_cannot_hide_a_prohibition_relaxation_from_affected_keys() -> None:
    with pytest.raises(ValueError, match="cannot propose relaxing V1 prohibitions"):
        _propose(
            affected_keys=("risk.max_open_positions",),
            proposed_values={"prohibited.automatic_risk_limit_changes": False},
        )


@pytest.mark.parametrize(
    "proposed_values",
    [
        {"prohibited": {"naked_options": False}},
        {"risk": {"prohibited": {"short_options": False}}},
        {"configuration": {"risk": {"prohibited": {"borrowed_margin": False}}}},
    ],
)
def test_reviewer_finds_prohibition_relaxations_in_nested_proposed_values(
    proposed_values: dict[str, object],
) -> None:
    with pytest.raises(ValueError, match="cannot propose relaxing V1 prohibitions"):
        _propose(
            affected_keys=("risk.max_open_positions",),
            proposed_values=proposed_values,
        )


@pytest.mark.parametrize("confidence", [Decimal("-0.01"), Decimal("1.01")])
def test_proposal_rejects_confidence_outside_the_unit_interval(confidence: Decimal) -> None:
    with pytest.raises(ValidationError):
        _propose(confidence=confidence)


@pytest.mark.parametrize("confidence", [Decimal("0"), Decimal("1")])
def test_proposal_accepts_confidence_boundaries(confidence: Decimal) -> None:
    assert _propose(confidence=confidence).confidence == confidence


def test_proposal_requires_a_timezone_aware_timestamp() -> None:
    with pytest.raises(ValidationError, match="timestamp must be timezone-aware"):
        _propose(created_at=datetime(2026, 9, 3, 15, 0))


def test_proposal_normalizes_an_aware_timestamp_to_utc() -> None:
    eastern = timezone(timedelta(hours=-4))

    proposal = _propose(created_at=datetime(2026, 9, 3, 11, 0, tzinfo=eastern))

    assert proposal.created_at == datetime(2026, 9, 3, 15, 0, tzinfo=UTC)


def test_learning_reviewer_exposes_no_apply_operation() -> None:
    assert not hasattr(LearningReviewer(), "apply")
