from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime
from decimal import Decimal
from typing import Any
from uuid import UUID, uuid4

from pydantic import Field

from app.domain.models import TimestampedModel


class ConfigurationChangeProposal(TimestampedModel):
    change_id: UUID = Field(default_factory=uuid4)
    evidence_sample_size: int = Field(gt=0)
    affected_keys: tuple[str, ...]
    proposed_values: dict[str, Any]
    before_replay_metrics: dict[str, Decimal]
    after_replay_metrics: dict[str, Decimal]
    expected_benefit: str
    possible_downside: str
    confidence: Decimal = Field(ge=0, le=1)
    applied: bool = False


class LearningReviewer:
    """Creates auditable proposals only; intentionally exposes no apply method."""

    minimum_sample_size = 30

    def propose(
        self,
        *,
        created_at: datetime,
        sample_size: int,
        affected_keys: tuple[str, ...],
        proposed_values: dict[str, Any],
        before: dict[str, Decimal],
        after: dict[str, Decimal],
        benefit: str,
        downside: str,
        confidence: Decimal,
    ) -> ConfigurationChangeProposal:
        if sample_size < self.minimum_sample_size:
            raise ValueError(f"at least {self.minimum_sample_size} observations are required")
        if _contains_prohibited_path(affected_keys, proposed_values):
            raise ValueError("learning cannot propose relaxing V1 prohibitions")
        return ConfigurationChangeProposal(
            created_at=created_at,
            evidence_sample_size=sample_size,
            affected_keys=affected_keys,
            proposed_values=proposed_values,
            before_replay_metrics=before,
            after_replay_metrics=after,
            expected_benefit=benefit,
            possible_downside=downside,
            confidence=confidence,
            applied=False,
        )


def _contains_prohibited_path(
    affected_keys: tuple[str, ...], proposed_values: Mapping[str, Any]
) -> bool:
    def prohibited(key: object) -> bool:
        return isinstance(key, str) and "prohibited" in key.lower().split(".")

    def mapping_has_prohibited_path(value: Mapping[str, Any]) -> bool:
        for key, child in value.items():
            if prohibited(key):
                return True
            if isinstance(child, Mapping) and mapping_has_prohibited_path(child):
                return True
        return False

    return any(prohibited(key) for key in affected_keys) or mapping_has_prohibited_path(
        proposed_values
    )
