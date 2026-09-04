from __future__ import annotations

import asyncio
import re
from collections.abc import Mapping, Sequence
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any, Literal, Protocol
from uuid import NAMESPACE_URL, UUID, uuid5

from pydantic import BaseModel, Field

from app.clock.base import Clock
from app.config import RuntimeBinding
from app.domain.enums import DemoBackend, ExecutionEnvironment
from app.domain.models import DomainModel, EquityQuote, OptionQuote, SentinelEvent, sha256_json
from app.exceptions import DataInvalidError, SafetyCriticalError, TransientError
from app.market.base import MarketDataProvider

_VERSION = "live-read-surveillance-v1"
type Quote = EquityQuote | OptionQuote


class LiveReadAuditRepository(Protocol):
    binding: RuntimeBinding

    async def healthcheck(self) -> bool: ...

    async def append(self, table: str, value: BaseModel | Mapping[str, Any]) -> UUID: ...

    async def find_payload(self, table: str, key: str, value: Any) -> dict[str, Any] | None: ...

    async def list_payloads(
        self,
        table: str,
        *,
        filters: Mapping[str, Any] | None = None,
        limit: int = 1000,
        before_sequence: int | None = None,
    ) -> list[dict[str, Any]]: ...


class LiveReadLimits(DomainModel):
    maximum_symbols: int = Field(default=20, ge=1, le=100)
    maximum_options_per_symbol: int = Field(default=200, ge=1, le=1000)
    maximum_quote_age_seconds: int = Field(default=120, ge=1, le=3600)
    maximum_baseline_age_seconds: int = Field(default=1800, ge=1, le=86400)
    tick_timeout_seconds: float = Field(default=30, gt=0, le=60)
    maximum_recovery_snapshots: int = Field(default=10000, ge=1, le=100000)


class _Observation(DomainModel):
    worker_version: Literal["live-read-surveillance-v1"] = "live-read-surveillance-v1"
    data_mode: Literal["LIVE_READ"] = "LIVE_READ"
    environment: Literal["DEMO"] = "DEMO"
    namespace: str
    surveillance_key: str
    observation_key: str
    stream_key: str
    created_at: datetime
    quote: EquityQuote | OptionQuote
    event: SentinelEvent | None


class LiveReadSurveillanceWorker:
    """Caller-paced, read-only BROKER_SHADOW equity/options surveillance.

    One process must own the environment lock. Snapshots contain their stable event
    before the event is separately committed, allowing bounded restart/retry repair
    without another provider response. Repeated quote IDs from adapters are not
    trusted for deduplication; IDs are derived from the complete observed content.
    Baseline events have severity zero and no measured-change claim. The caller may
    pass returned events to an idempotent research worker; this class never invokes
    models, account APIs, authentication, or order/execution methods.

    Returned events are at-least-once across an interrupted tick. The durable event
    identity is stable, but this is not a distributed exactly-once outbox. Recovery
    fails explicitly above its historical row budget; it never silently drops rows.
    """

    def __init__(
        self,
        market: MarketDataProvider,
        clock: Clock,
        repository: LiveReadAuditRepository,
        *,
        watchlist: Sequence[str],
        limits: LiveReadLimits | None = None,
    ) -> None:
        self.market, self.clock, self.repository = market, clock, repository
        self.limits = limits or LiveReadLimits()
        if isinstance(watchlist, str) or not 1 <= len(watchlist) <= self.limits.maximum_symbols:
            raise ValueError("an explicit bounded watchlist is required")
        normalized = tuple(symbol.strip().upper() for symbol in watchlist)
        if len(set(normalized)) != len(normalized) or any(
            re.fullmatch(r"[A-Z0-9][A-Z0-9.-]{0,11}", symbol) is None for symbol in normalized
        ):
            raise ValueError("watchlist symbols must be unique and unambiguous")
        self.watchlist = tuple(sorted(normalized))
        self.binding = repository.binding
        self._identity, self._capability_version = market.identity, market.capability_version
        self._guard()
        self._key = sha256_json(
            {
                "worker": _VERSION,
                "namespace": self.binding.idempotency_namespace,
                "provider": self._identity,
                "capability_version": self._capability_version,
                "watchlist": self.watchlist,
                "limits": self.limits.model_dump(mode="json"),
            }
        )
        self._lock = asyncio.Lock()
        self._restored = False
        self._previous: dict[str, _Observation] = {}
        self.last_scan_healthy = False
        self._last_successful_quotes: tuple[Quote, ...] = ()
        self._last_successful_at: datetime | None = None

    def _guard(self) -> None:
        if (
            self.repository.binding != self.binding
            or self.binding.environment is not ExecutionEnvironment.DEMO
            or self.binding.demo_backend is not DemoBackend.BROKER_SHADOW
            or self.binding.external_write_authority
        ):
            raise SafetyCriticalError("live reads require the no-write BROKER_SHADOW binding")
        capabilities = self.market.capabilities
        if capabilities.replay or not capabilities.equity_quotes or not capabilities.option_chains:
            raise SafetyCriticalError("current equity and option-chain read capabilities required")
        if (
            not self._identity.strip()
            or not self._capability_version.strip()
            or self.market.identity != self._identity
            or self.market.capability_version != self._capability_version
        ):
            raise SafetyCriticalError("market provider identity or capability version changed")

    async def scan(self) -> tuple[SentinelEvent, ...]:
        return await self.tick()

    async def health(self) -> bool:
        """Recheck actual quote ages on every call; never extend freshness to poll time."""
        try:
            self._guard()
            if (
                not self.last_scan_healthy
                or not self._last_successful_quotes
                or self._last_successful_at is None
            ):
                return False
            for quote in self._last_successful_quotes:
                self._validate_quote(
                    quote,
                    self._symbol(quote),
                    self._last_successful_at,
                    option=isinstance(quote, OptionQuote),
                )
            return True
        except Exception:
            self.last_scan_healthy = False
            return False

    async def tick(self) -> tuple[SentinelEvent, ...]:
        async with self._lock:
            self.last_scan_healthy = False
            try:
                async with asyncio.timeout(self.limits.tick_timeout_seconds):
                    self._guard()
                    if not await self.repository.healthcheck():
                        raise SafetyCriticalError(
                            "market surveillance requires writable audit storage"
                        )
                    events = await self._restore() if not self._restored else []
                    cutoff = self.clock.now()
                    observations: list[Quote] = []
                    for symbol in self.watchlist:
                        equity = await self.market.get_equity_quote(symbol, as_of=cutoff)
                        self._validate_quote(equity, symbol, cutoff, option=False)
                        observations.append(equity)
                        chain = await self.market.get_option_chain(symbol, as_of=cutoff)
                        if not isinstance(chain, Sequence) or not (
                            1 <= len(chain) <= self.limits.maximum_options_per_symbol
                        ):
                            raise DataInvalidError("option chain is empty or exceeds its budget")
                        instruments: set[str] = set()
                        for option_quote in chain:
                            self._validate_quote(option_quote, symbol, cutoff, option=True)
                            if option_quote.contract.instrument_id in instruments:
                                raise DataInvalidError("duplicate option instrument in one chain")
                            instruments.add(option_quote.contract.instrument_id)
                            observations.append(option_quote)
                    # A slow later request must not make earlier stale quotes look fresh.
                    for quote in observations:
                        self._validate_quote(
                            quote,
                            self._symbol(quote),
                            cutoff,
                            option=isinstance(quote, OptionQuote),
                        )
                    for quote in observations:
                        event = await self._persist(quote)
                        if event is not None:
                            events.append(event)
                    await self._health(True, len(observations), len(events))
                    self._last_successful_quotes = tuple(observations)
                    self._last_successful_at = self.clock.now()
                    self.last_scan_healthy = True
                    return tuple(events)
            except asyncio.CancelledError:
                self._restored = False
                raise
            except Exception as exc:
                self._restored = False
                try:
                    async with asyncio.timeout(2):
                        await self._health(False, 0, 0, type(exc).__name__)
                except Exception:
                    self.last_scan_healthy = False  # Preserve the primary, sanitized failure.
                if isinstance(exc, SafetyCriticalError):
                    raise SafetyCriticalError(
                        "read-only market scan failed its safety checks"
                    ) from None
                if isinstance(exc, DataInvalidError):
                    raise DataInvalidError(
                        "read-only market scan received invalid evidence"
                    ) from None
                raise TransientError(
                    "read-only market scan failed or exceeded its deadline"
                ) from None

    def _validate_quote(
        self,
        quote: Quote,
        symbol: str,
        cutoff: datetime,
        *,
        option: bool,
    ) -> None:
        expected = OptionQuote if option else EquityQuote
        if not isinstance(quote, expected) or self._symbol(quote) != symbol:
            raise DataInvalidError("provider returned an unrelated quote")
        metadata = quote.metadata
        now = self.clock.now()
        if (
            metadata.provider != self._identity
            or metadata.capability_version != self._capability_version
            or not metadata.effective_at <= metadata.observed_at <= cutoff <= now
            or now - metadata.effective_at
            > timedelta(seconds=self.limits.maximum_quote_age_seconds)
        ):
            raise DataInvalidError("quote provenance, causal ordering, or freshness failed")
        for value in quote.model_dump().values():
            if isinstance(value, Decimal) and not value.is_finite():
                raise DataInvalidError("quote contains a non-finite number")
        if quote.ask <= 0 or quote.bid > quote.ask:
            raise DataInvalidError("quote does not contain a valid bid/ask market")
        if isinstance(quote, EquityQuote) and quote.last <= 0:
            raise DataInvalidError("equity last price must be positive")
        if isinstance(quote, OptionQuote) and (
            quote.contract.expiration < metadata.effective_at.date()
            or not quote.contract.strike.is_finite()
        ):
            raise DataInvalidError("option contract is expired or invalid")

    @staticmethod
    def _symbol(quote: Quote) -> str:
        return quote.contract.symbol if isinstance(quote, OptionQuote) else quote.symbol

    @staticmethod
    def _stream(quote: Quote) -> str:
        return (
            f"option:{quote.contract.instrument_id}"
            if isinstance(quote, OptionQuote)
            else f"equity:{quote.symbol}"
        )

    def _observation_key(self, quote: Quote) -> str:
        digest = sha256_json(quote.model_dump(mode="json", exclude={"snapshot_id"}))
        return f"live-read:{self._key}:{digest}"

    async def _restore(self) -> list[SentinelEvent]:
        self._previous = {}
        repaired: list[SentinelEvent] = []
        count = 0
        for table in ("market_snapshots", "option_snapshots"):
            before: int | None = None
            while True:
                rows = await self.repository.list_payloads(
                    table,
                    filters={
                        "surveillance_key": self._key,
                        "namespace": self.binding.idempotency_namespace,
                        "environment": "DEMO",
                    },
                    limit=1000,
                    before_sequence=before,
                )
                if not rows:
                    break
                count += len(rows)
                if count > self.limits.maximum_recovery_snapshots:
                    raise SafetyCriticalError("market snapshot recovery budget exhausted")
                for row in rows:
                    stored = _Observation.model_validate(row["payload"])
                    quote = stored.quote
                    if (
                        stored.namespace != self.binding.idempotency_namespace
                        or stored.surveillance_key != self._key
                        or stored.stream_key != self._stream(quote)
                        or self._symbol(quote) not in self.watchlist
                        or stored.observation_key != self._observation_key(quote)
                        or quote.snapshot_id != uuid5(NAMESPACE_URL, stored.observation_key)
                        or quote.metadata.provider != self._identity
                        or quote.metadata.capability_version != self._capability_version
                        or not quote.metadata.effective_at
                        <= quote.metadata.observed_at
                        <= stored.created_at
                        <= self.clock.now()
                    ):
                        raise SafetyCriticalError(
                            "stored market observation failed provenance validation"
                        )
                    current = self._previous.get(stored.stream_key)
                    if current is None or self._recency(stored) > self._recency(current):
                        self._previous[stored.stream_key] = stored
                    if stored.event is not None:
                        await self._ensure_event(stored)
                        # A prior process may have committed the event and crashed before
                        # returning it. Re-offer eligible events; consumers deduplicate IDs.
                        age = self.clock.now() - stored.event.effective_at
                        if (
                            timedelta(0)
                            <= age
                            <= timedelta(seconds=self.limits.maximum_quote_age_seconds)
                        ):
                            repaired.append(stored.event)
                next_before = min(int(row["append_sequence"]) for row in rows)
                if before is not None and next_before >= before:
                    raise SafetyCriticalError("market recovery pagination did not advance")
                before = next_before
                if len(rows) < 1000:
                    break
        self._restored = True
        return sorted(repaired, key=lambda event: (event.created_at, str(event.event_id)))

    @staticmethod
    def _recency(stored: _Observation) -> tuple[datetime, datetime, datetime]:
        return (
            stored.quote.metadata.effective_at,
            stored.quote.metadata.observed_at,
            stored.created_at,
        )

    async def _persist(self, quote: Quote) -> SentinelEvent | None:
        key, stream = self._observation_key(quote), self._stream(quote)
        quote = quote.model_copy(update={"snapshot_id": uuid5(NAMESPACE_URL, key)})
        table = "option_snapshots" if isinstance(quote, OptionQuote) else "market_snapshots"
        previous = self._previous.get(stream)
        if previous is not None:
            old = previous.quote
            if (
                quote.metadata.effective_at < old.metadata.effective_at
                or quote.metadata.observed_at < old.metadata.observed_at
                or isinstance(quote, OptionQuote)
                and isinstance(old, OptionQuote)
                and quote.contract != old.contract
            ):
                raise DataInvalidError("market stream or contract identity moved backward")
        existing = await self.repository.list_payloads(
            table,
            filters={
                "observation_key": key,
                "namespace": self.binding.idempotency_namespace,
                "environment": "DEMO",
            },
            limit=1,
        )
        if existing:
            stored = _Observation.model_validate(existing[0]["payload"])
            await self._ensure_event(stored)
            return None
        changes = self._changes(previous.quote, quote) if previous is not None else {}
        baseline = previous is None or (
            quote.metadata.effective_at - previous.quote.metadata.effective_at
            > timedelta(seconds=self.limits.maximum_baseline_age_seconds)
        )
        if (
            previous is not None
            and changes
            and (quote.metadata.effective_at == previous.quote.metadata.effective_at)
        ):
            raise DataInvalidError("conflicting market values share one effective timestamp")
        now = self.clock.now()
        event: SentinelEvent | None = None
        if baseline or changes:
            event_key = key + ":event"
            references: tuple[str, ...] = (str(quote.snapshot_id),)
            if not baseline:
                assert previous is not None
                references = (str(previous.quote.snapshot_id), str(quote.snapshot_id))
            event = SentinelEvent(
                event_id=uuid5(NAMESPACE_URL, event_key),
                created_at=now,
                event_type="MARKET_BASELINE" if baseline else "MARKET_MEASURED_CHANGE",
                source=self._identity,
                effective_at=quote.metadata.effective_at,
                tickers=(self._symbol(quote),),
                severity=0 if baseline else 1,
                deduplication_key=event_key,
                raw_reference_ids=references,
                payload={
                    "data_mode": "LIVE_READ",
                    "worker_version": _VERSION,
                    "quote_kind": "option" if isinstance(quote, OptionQuote) else "equity",
                    "provider": self._identity,
                    "capability_version": self._capability_version,
                    "baseline": baseline,
                    "changes": {} if baseline else changes,
                },
            )
        stored = _Observation(
            namespace=self.binding.idempotency_namespace,
            surveillance_key=self._key,
            observation_key=key,
            stream_key=stream,
            created_at=now,
            quote=quote,
            event=event,
        )
        await self.repository.append(table, stored)
        await self._ensure_event(stored)
        self._previous[stream] = stored
        return event

    @staticmethod
    def _changes(previous: Quote, current: Quote) -> dict[str, Any]:
        fields = (
            ("bid", "ask", "last", "volume")
            if isinstance(current, EquityQuote)
            else (
                "bid",
                "ask",
                "last",
                "mark",
                "volume",
                "open_interest",
                "implied_volatility",
            )
        )
        changes: dict[str, Any] = {}
        for name in fields:
            old, new = getattr(previous, name), getattr(current, name)
            # Unknown values becoming available are not measured changes.
            if old is not None and new is not None and old != new:
                changes[name] = {"previous": str(old), "current": str(new), "delta": str(new - old)}
        return changes

    async def _ensure_event(self, stored: _Observation) -> bool:
        event = stored.event
        if event is None:
            return False
        if (
            event.event_id != uuid5(NAMESPACE_URL, stored.observation_key + ":event")
            or event.deduplication_key != stored.observation_key + ":event"
            or str(stored.quote.snapshot_id) not in event.raw_reference_ids
            or event.created_at != stored.created_at
            or event.effective_at != stored.quote.metadata.effective_at
            or event.source != self._identity
            or event.payload.get("data_mode") != "LIVE_READ"
        ):
            raise SafetyCriticalError("market snapshot event identity is inconsistent")
        existing = await self.repository.find_payload(
            "sentinel_events", "deduplication_key", event.deduplication_key
        )
        if existing is not None:
            expected = {
                **event.model_dump(mode="json"),
                "environment": "DEMO",
                "namespace": self.binding.idempotency_namespace,
            }
            if existing["payload"] != expected:
                raise SafetyCriticalError("stored market event has conflicting content")
            return False
        await self.repository.append(
            "sentinel_events",
            {
                **event.model_dump(mode="json"),
                "environment": "DEMO",
                "namespace": self.binding.idempotency_namespace,
            },
        )
        return True

    async def _health(self, healthy: bool, quotes: int, events: int, failure: str = "") -> None:
        await self.repository.append(
            "health_events",
            {
                "created_at": self.clock.now(),
                "environment": "DEMO",
                "namespace": self.binding.idempotency_namespace,
                "component": "live_read_surveillance",
                "worker_version": _VERSION,
                "data_mode": "LIVE_READ",
                "healthy": healthy,
                "provider": self._identity,
                "capability_version": self._capability_version,
                "watchlist_size": len(self.watchlist),
                "quotes_received": quotes,
                "events_emitted": events,
                "failure_type": failure or None,
            },
        )
