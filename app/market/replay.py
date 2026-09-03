from __future__ import annotations

import json
from collections.abc import Iterable, Sequence
from datetime import UTC, datetime
from pathlib import Path

from pydantic import ValidationError

from app.clock.base import Clock
from app.domain.models import EquityQuote, OptionQuote
from app.exceptions import DataInvalidError
from app.market.base import MarketDataProvider
from app.market.models import (
    EquityScanRequest,
    MarketDataCapabilities,
    PriceBar,
    ReplayFixture,
    ReplayRecord,
)


class MarketDataNotFoundError(DataInvalidError):
    """No causally available record satisfies a market-data request."""


class LookaheadViolationError(DataInvalidError):
    """A caller attempted to inspect replay state beyond the injected clock."""


class OfflineReplayMarketDataProvider(MarketDataProvider):
    """Immutable, timestamp-gated fixture provider for regression and replay."""

    def __init__(self, fixture: ReplayFixture, clock: Clock) -> None:
        self._fixture = fixture
        self._clock = clock
        self._records = tuple(sorted(fixture.records, key=lambda item: item.sequence))
        self._validate_payloads()

    @classmethod
    def from_path(cls, path: Path, clock: Clock) -> OfflineReplayMarketDataProvider:
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
            fixture = ReplayFixture.model_validate(raw)
        except (OSError, json.JSONDecodeError, ValidationError) as exc:
            raise DataInvalidError(f"invalid replay fixture {path}: {exc}") from exc
        return cls(fixture, clock)

    @property
    def identity(self) -> str:
        return self._fixture.provider

    @property
    def capability_version(self) -> str:
        return self._fixture.capability_version

    @property
    def capabilities(self) -> MarketDataCapabilities:
        return MarketDataCapabilities(replay=True)

    def _cutoff(self, as_of: datetime | None) -> datetime:
        now = self._clock.now()
        value = now if as_of is None else _as_utc(as_of, "as_of")
        if value > now:
            raise LookaheadViolationError(
                f"requested replay time {value.isoformat()} exceeds clock {now.isoformat()}"
            )
        return value

    def _visible(self, kind: str, cutoff: datetime) -> tuple[ReplayRecord, ...]:
        return tuple(
            record
            for record in self._records
            if record.kind == kind
            and record.observed_at <= cutoff
            and record.effective_at <= cutoff
        )

    def visible_records(self, *, as_of: datetime | None = None) -> tuple[ReplayRecord, ...]:
        cutoff = self._cutoff(as_of)
        return tuple(
            record
            for record in self._records
            if record.observed_at <= cutoff and record.effective_at <= cutoff
        )

    def next_available_at(self) -> datetime | None:
        now = self._clock.now()
        return next(
            (record.available_at for record in self._records if record.available_at > now),
            None,
        )

    async def get_equity_quote(self, symbol: str, *, as_of: datetime | None = None) -> EquityQuote:
        cutoff = self._cutoff(as_of)
        normalized = _symbol(symbol)
        matches = [
            (record, EquityQuote.model_validate(record.payload))
            for record in self._visible("equity_quote", cutoff)
            if str(record.payload.get("symbol", "")).upper() == normalized
        ]
        return _latest(matches, f"equity quote for {normalized}")

    async def get_bars(
        self,
        symbol: str,
        *,
        start: datetime | None = None,
        end: datetime | None = None,
        timeframe: str | None = None,
        as_of: datetime | None = None,
    ) -> Sequence[PriceBar]:
        cutoff = self._cutoff(as_of)
        normalized = _symbol(symbol)
        start_utc = _as_utc(start, "start") if start is not None else None
        end_utc = _as_utc(end, "end") if end is not None else cutoff
        if end_utc > cutoff:
            raise LookaheadViolationError("bar end exceeds the causally available cutoff")
        if start_utc is not None and start_utc > end_utc:
            raise ValueError("bar start exceeds end")
        bars: list[PriceBar] = []
        for record in self._visible("bar", cutoff):
            if str(record.payload.get("symbol", "")).upper() != normalized:
                continue
            bar = PriceBar.model_validate(record.payload)
            if timeframe is not None and bar.timeframe != timeframe:
                continue
            if start_utc is not None and bar.ends_at < start_utc:
                continue
            if bar.ends_at > end_utc:
                continue
            bars.append(bar)
        bars.sort(key=lambda bar: (bar.ends_at, bar.starts_at))
        return tuple(bars)

    async def get_option_chain(
        self, symbol: str, *, as_of: datetime | None = None
    ) -> Sequence[OptionQuote]:
        cutoff = self._cutoff(as_of)
        normalized = _symbol(symbol)
        latest_by_instrument: dict[str, tuple[ReplayRecord, OptionQuote]] = {}
        for record in self._visible("option_quote", cutoff):
            contract = record.payload.get("contract", {})
            if str(contract.get("symbol", "")).upper() != normalized:
                continue
            quote = OptionQuote.model_validate(record.payload)
            existing = latest_by_instrument.get(quote.contract.instrument_id)
            if existing is None or _recency(record) > _recency(existing[0]):
                latest_by_instrument[quote.contract.instrument_id] = (record, quote)
        return tuple(
            item[1]
            for item in sorted(
                latest_by_instrument.values(),
                key=lambda pair: (
                    pair[1].contract.expiration,
                    pair[1].contract.option_type.value,
                    pair[1].contract.strike,
                    pair[1].contract.instrument_id,
                ),
            )
        )

    async def get_option_quote(
        self, instrument_id: str, *, as_of: datetime | None = None
    ) -> OptionQuote:
        cutoff = self._cutoff(as_of)
        matches = [
            (record, OptionQuote.model_validate(record.payload))
            for record in self._visible("option_quote", cutoff)
            if str(record.payload.get("contract", {}).get("instrument_id", "")) == instrument_id
        ]
        return _latest(matches, f"option quote for {instrument_id}")

    async def scan_equities(
        self, request: EquityScanRequest, *, as_of: datetime | None = None
    ) -> Sequence[EquityQuote]:
        cutoff = self._cutoff(as_of)
        allowed = {_symbol(symbol) for symbol in request.symbols}
        latest_by_symbol: dict[str, tuple[ReplayRecord, EquityQuote]] = {}
        for record in self._visible("equity_quote", cutoff):
            quote = EquityQuote.model_validate(record.payload)
            if allowed and quote.symbol.upper() not in allowed:
                continue
            existing = latest_by_symbol.get(quote.symbol.upper())
            if existing is None or _recency(record) > _recency(existing[0]):
                latest_by_symbol[quote.symbol.upper()] = (record, quote)
        results = []
        for _, quote in latest_by_symbol.values():
            price = quote.last
            if request.minimum_price is not None and price < request.minimum_price:
                continue
            if request.maximum_price is not None and price > request.maximum_price:
                continue
            if request.minimum_volume is not None and quote.volume < request.minimum_volume:
                continue
            results.append(quote)
        results.sort(key=lambda quote: (-quote.volume, quote.symbol))
        return tuple(results[: request.limit])

    def _validate_payloads(self) -> None:
        try:
            for record in self._records:
                value: EquityQuote | OptionQuote | PriceBar
                if record.kind == "equity_quote":
                    value = EquityQuote.model_validate(record.payload)
                elif record.kind == "option_quote":
                    value = OptionQuote.model_validate(record.payload)
                else:
                    value = PriceBar.model_validate(record.payload)
                metadata = value.metadata
                if metadata.provider != self.identity:
                    raise ValueError("payload provider differs from fixture provider")
                if metadata.capability_version != self.capability_version:
                    raise ValueError("payload capability version differs from fixture")
                if metadata.observed_at != record.observed_at:
                    raise ValueError("payload observed_at differs from replay record")
                if metadata.effective_at != record.effective_at:
                    raise ValueError("payload effective_at differs from replay record")
        except (ValidationError, ValueError) as exc:
            raise DataInvalidError(f"invalid replay payload: {exc}") from exc


def _as_utc(value: datetime, name: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{name} must be timezone-aware")
    return value.astimezone(UTC)


def _symbol(value: str) -> str:
    normalized = value.strip().upper()
    if not normalized:
        raise ValueError("symbol cannot be empty")
    return normalized


def _recency(record: ReplayRecord) -> tuple[datetime, datetime, int]:
    return (record.effective_at, record.observed_at, record.sequence)


def _latest[TMarketObject: (EquityQuote, OptionQuote, PriceBar)](
    values: Iterable[tuple[ReplayRecord, TMarketObject]], description: str
) -> TMarketObject:
    materialized = tuple(values)
    if not materialized:
        raise MarketDataNotFoundError(f"no visible {description}")
    return max(materialized, key=lambda pair: _recency(pair[0]))[1]
