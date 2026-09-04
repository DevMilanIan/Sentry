from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal
from enum import StrEnum
from urllib.parse import urlsplit
from uuid import UUID, uuid4

from pydantic import Field, StrictBool, field_validator, model_validator

from app.domain.models import DomainModel, TimestampedModel


class RelationshipType(StrEnum):
    COMMON_EQUITY = "COMMON_EQUITY"
    WARRANT_OR_PREFERRED = "WARRANT_OR_PREFERRED"
    STRATEGIC_INVESTMENT = "STRATEGIC_INVESTMENT"
    MAJOR_FINANCING = "MAJOR_FINANCING"
    CONTRACT_OR_PROGRAM = "CONTRACT_OR_PROGRAM"
    INDIRECT_POLICY = "INDIRECT_POLICY"
    NONE = "NONE"


class FederalRelationshipDetails(DomainModel):
    ticker: str = Field(pattern=r"^[A-Z][A-Z0-9.-]{0,14}$")
    issuer_name: str = Field(min_length=1, max_length=240)
    agency: str = Field(min_length=1, max_length=160)
    relationship_type: RelationshipType
    announcement_date: date
    effective_date: date | None = None
    end_date: date | None = None
    equity_ownership_percent: Decimal | None = Field(
        default=None, ge=0, le=100, max_digits=9, decimal_places=6
    )
    warrant_or_preferred_exposure: str | None = Field(default=None, max_length=2000)
    financing_amount: Decimal | None = Field(
        default=None, ge=0, le=Decimal("1e15"), max_digits=20, decimal_places=4
    )
    contract_program_amount: Decimal | None = Field(
        default=None, ge=0, le=Decimal("1e15"), max_digits=20, decimal_places=4
    )
    strategic_designation: str | None = Field(default=None, max_length=1000)
    primary_source_url: str = Field(min_length=1, max_length=2048)
    source_publication_date: date
    confidence: Decimal = Field(ge=0, le=1, max_digits=6, decimal_places=5)
    active: StrictBool = True
    notes: str = Field(default="", max_length=4000)
    last_verified_at: datetime | None = None
    source_available_at: datetime | None = None

    @field_validator("issuer_name", "agency")
    @classmethod
    def meaningful_name(cls, value: str) -> str:
        if not value.strip() or any(ord(character) < 32 for character in value):
            raise ValueError("issuer and agency names must be nonblank plain text")
        return value

    @field_validator("last_verified_at", "source_available_at")
    @classmethod
    def aware_timestamp(cls, value: datetime | None) -> datetime | None:
        if value is not None:
            if value.tzinfo is None or value.utcoffset() is None:
                raise ValueError("registry timestamps must be timezone-aware")
            return value.astimezone(UTC)
        return value

    @field_validator("primary_source_url")
    @classmethod
    def safe_https_uri(cls, value: str) -> str:
        if not value.isascii() or any(ord(character) <= 32 for character in value) or "\\" in value:
            raise ValueError("primary source must be an ASCII HTTPS URI without whitespace")
        parts = urlsplit(value)
        if (
            parts.scheme != "https"
            or not parts.hostname
            or parts.username is not None
            or parts.password is not None
            or parts.port not in (None, 443)
            or parts.fragment
            or parts.hostname.endswith(".")
        ):
            raise ValueError("primary source must be credential-free HTTPS on the standard port")
        return value

    @model_validator(mode="after")
    def date_range_is_ordered(self) -> FederalRelationshipDetails:
        if self.effective_date and self.end_date and self.end_date < self.effective_date:
            raise ValueError("federal relationship end date precedes effective date")
        return self


class FederalRelationship(TimestampedModel, FederalRelationshipDetails):
    relationship_id: UUID = Field(default_factory=uuid4)


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
