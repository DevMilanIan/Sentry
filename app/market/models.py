from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from typing import Any, Literal

from pydantic import Field, field_validator, model_validator

from app.domain.models import DomainModel, ProviderMetadata


class MarketDataCapabilities(DomainModel):
    """Features implemented by a market-data adapter."""

    equity_quotes: bool = True
    equity_bars: bool = True
    option_chains: bool = True
    option_quotes: bool = True
    equity_scans: bool = True
    replay: bool = False


class PriceBar(DomainModel):
    symbol: str = Field(min_length=1, max_length=12)
    timeframe: str = Field(min_length=1, max_length=16)
    starts_at: datetime
    ends_at: datetime
    open: Decimal = Field(ge=0)
    high: Decimal = Field(ge=0)
    low: Decimal = Field(ge=0)
    close: Decimal = Field(ge=0)
    volume: int = Field(ge=0)
    metadata: ProviderMetadata

    @field_validator("starts_at", "ends_at")
    @classmethod
    def timestamps_are_utc(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("bar timestamps must be timezone-aware")
        return value.astimezone(UTC)

    @model_validator(mode="after")
    def valid_bar(self) -> PriceBar:
        if self.ends_at <= self.starts_at:
            raise ValueError("bar must end after it starts")
        if self.high < max(self.open, self.close, self.low):
            raise ValueError("bar high is below an OHLC value")
        if self.low > min(self.open, self.close, self.high):
            raise ValueError("bar low is above an OHLC value")
        if self.metadata.effective_at != self.ends_at:
            raise ValueError("bar effective_at must equal ends_at")
        return self


class EquityScanRequest(DomainModel):
    symbols: tuple[str, ...] = ()
    minimum_price: Decimal | None = Field(default=None, ge=0)
    maximum_price: Decimal | None = Field(default=None, ge=0)
    minimum_volume: int | None = Field(default=None, ge=0)
    limit: int = Field(default=100, ge=1, le=10_000)

    @model_validator(mode="after")
    def price_range_is_ordered(self) -> EquityScanRequest:
        if (
            self.minimum_price is not None
            and self.maximum_price is not None
            and self.minimum_price > self.maximum_price
        ):
            raise ValueError("minimum_price exceeds maximum_price")
        return self


ReplayKind = Literal["equity_quote", "option_quote", "bar"]


class ReplayRecord(DomainModel):
    """A fixture observation ordered by when it became visible to the system."""

    sequence: int = Field(ge=0)
    kind: ReplayKind
    observed_at: datetime
    effective_at: datetime
    payload: dict[str, Any]

    @field_validator("observed_at", "effective_at")
    @classmethod
    def timestamps_are_utc(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("replay timestamps must be timezone-aware")
        return value.astimezone(UTC)

    @model_validator(mode="after")
    def observation_is_not_early(self) -> ReplayRecord:
        if self.observed_at < self.effective_at:
            raise ValueError("a fixture cannot be observed before it is effective")
        return self

    @property
    def available_at(self) -> datetime:
        return max(self.observed_at, self.effective_at)


class ReplayFixture(DomainModel):
    version: str = Field(min_length=1)
    provider: str = Field(min_length=1)
    capability_version: str = Field(min_length=1)
    records: tuple[ReplayRecord, ...]

    @model_validator(mode="after")
    def deterministic_order(self) -> ReplayFixture:
        sequences = [record.sequence for record in self.records]
        if len(sequences) != len(set(sequences)):
            raise ValueError("replay record sequence values must be unique")
        ordered = sorted(self.records, key=lambda item: item.sequence)
        for previous, current in zip(ordered, ordered[1:], strict=False):
            if current.observed_at < previous.observed_at:
                raise ValueError("replay sequence must follow observed_at ordering")
        return self
