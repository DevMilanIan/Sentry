from __future__ import annotations

from decimal import Decimal
from enum import StrEnum

from pydantic import Field, model_validator

from app.domain.enums import AttentionLevel
from app.domain.models import DomainModel


class AttentionStage(StrEnum):
    SURVEILLANCE = "SURVEILLANCE"
    POST_REASONING = "POST_REASONING"


class AttentionThresholds(DomainModel):
    version: str = "attention-v1"
    watch: Decimal = Field(default=Decimal("25"), ge=0, le=100)
    candidate: Decimal = Field(default=Decimal("50"), ge=0, le=100)
    trade_worthy: Decimal = Field(default=Decimal("70"), ge=0, le=100)
    deep_research: Decimal = Field(default=Decimal("85"), ge=0, le=100)

    @model_validator(mode="after")
    def ordered(self) -> AttentionThresholds:
        if not self.watch <= self.candidate <= self.trade_worthy <= self.deep_research:
            raise ValueError("attention thresholds must be monotonically increasing")
        return self


class AttentionMapper:
    def __init__(self, thresholds: AttentionThresholds | None = None) -> None:
        self.thresholds = thresholds or AttentionThresholds()

    def map(
        self,
        score: Decimal,
        *,
        stage: AttentionStage = AttentionStage.SURVEILLANCE,
        has_position: bool = False,
    ) -> AttentionLevel:
        if not Decimal("0") <= score <= Decimal("100"):
            raise ValueError("attention score must be in [0, 100]")
        if has_position:
            return AttentionLevel.POSITION
        if score < self.thresholds.watch:
            level = AttentionLevel.BACKGROUND
        elif score < self.thresholds.candidate:
            level = AttentionLevel.WATCH
        elif score < self.thresholds.trade_worthy:
            level = AttentionLevel.CANDIDATE
        elif score < self.thresholds.deep_research:
            level = AttentionLevel.TRADE_WORTHY
        else:
            level = AttentionLevel.DEEP_RESEARCH
        # Surveillance ranking allocates model attention; it cannot declare a trade worthy.
        if stage is AttentionStage.SURVEILLANCE and level > AttentionLevel.CANDIDATE:
            return AttentionLevel.CANDIDATE
        return level
