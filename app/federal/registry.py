from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from enum import StrEnum
from uuid import UUID, uuid4

from pydantic import Field, model_validator

from app.domain.models import DomainModel, TimestampedModel


class RelationshipType(StrEnum):
    COMMON_EQUITY = "COMMON_EQUITY"
    WARRANT_OR_PREFERRED = "WARRANT_OR_PREFERRED"
    STRATEGIC_INVESTMENT = "STRATEGIC_INVESTMENT"
    MAJOR_FINANCING = "MAJOR_FINANCING"
    CONTRACT_OR_PROGRAM = "CONTRACT_OR_PROGRAM"
    INDIRECT_POLICY = "INDIRECT_POLICY"
    NONE = "NONE"


class FederalRelationship(TimestampedModel):
    relationship_id: UUID = Field(default_factory=uuid4)
    ticker: str
    issuer_name: str
    agency: str
    relationship_type: RelationshipType
    announcement_date: date
    effective_date: date | None = None
    end_date: date | None = None
    equity_ownership_percent: Decimal | None = Field(default=None, ge=0, le=100)
    warrant_or_preferred_exposure: str | None = None
    financing_amount: Decimal | None = Field(default=None, ge=0)
    contract_program_amount: Decimal | None = Field(default=None, ge=0)
    strategic_designation: str | None = None
    primary_source_url: str
    source_publication_date: date
    confidence: Decimal = Field(ge=0, le=1)
    active: bool = True
    notes: str = ""
    last_verified_at: datetime

    @model_validator(mode="after")
    def date_range_is_ordered(self) -> FederalRelationship:
        if self.effective_date and self.end_date and self.end_date < self.effective_date:
            raise ValueError("federal relationship end date precedes effective date")
        return self


class ExposureScore(DomainModel):
    value: Decimal = Field(ge=0, le=100)
    version: str
    explanation: tuple[str, ...]


class FederalExposureScorer:
    version = "federal-exposure-v1"
    _base = {
        RelationshipType.COMMON_EQUITY: Decimal("95"),
        RelationshipType.WARRANT_OR_PREFERRED: Decimal("85"),
        RelationshipType.STRATEGIC_INVESTMENT: Decimal("82"),
        RelationshipType.MAJOR_FINANCING: Decimal("72"),
        RelationshipType.CONTRACT_OR_PROGRAM: Decimal("55"),
        RelationshipType.INDIRECT_POLICY: Decimal("30"),
        RelationshipType.NONE: Decimal("0"),
    }

    def score(self, relationships: list[FederalRelationship]) -> ExposureScore:
        active = [relationship for relationship in relationships if relationship.active]
        if not active:
            return ExposureScore(
                value=Decimal("0"),
                version=self.version,
                explanation=("no active verified relationship",),
            )
        components: list[tuple[Decimal, FederalRelationship]] = []
        for relationship in active:
            base = self._base[relationship.relationship_type]
            confidence_adjusted = base * relationship.confidence
            components.append((confidence_adjusted, relationship))
        components.sort(key=lambda item: item[0], reverse=True)
        highest = components[0][0]
        diversity_bonus = min(
            Decimal("5"), Decimal(max(0, len({item[1].agency for item in components}) - 1) * 2)
        )
        value = min(Decimal("100"), highest + diversity_bonus).quantize(Decimal("0.01"))
        explanation = tuple(
            (
                f"{relationship.agency}:{relationship.relationship_type.value} "
                f"confidence={relationship.confidence}"
            )
            for _, relationship in components[:5]
        )
        return ExposureScore(value=value, version=self.version, explanation=explanation)
