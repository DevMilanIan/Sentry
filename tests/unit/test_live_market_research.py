from __future__ import annotations

import asyncio
from collections.abc import Mapping, Sequence
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any
from uuid import UUID

import pytest
from pydantic import BaseModel

from app.clock.base import VirtualClock
from app.config import RuntimeBinding, load_config
from app.db.repository import InMemoryAuditRepository
from app.demo.offline_scenario import _scripted_outputs
from app.domain.enums import DemoBackend, ExecutionEnvironment
from app.domain.models import (
    EquityQuote,
    OptionQuote,
    ProviderMetadata,
    SentinelEvent,
    TradeProposal,
    sha256_json,
)
from app.exceptions import DataInvalidError, SafetyCriticalError
from app.market.base import MarketDataProvider
from app.market.models import EquityScanRequest, MarketDataCapabilities, PriceBar
from app.reasoning.policies import load_decision_policy_set
from app.reasoning.scripted import ScriptedReplayModelProvider
from app.sentinel.live_reads import LiveReadSurveillanceWorker
from app.strategy.live_research import LiveMarketResearchQueue
from app.strategy.runtime import CandidateResearchWorker


class FixtureMarket(MarketDataProvider):
    def __init__(self) -> None:
        self.version = "fixture-live-v1"
        self.quotes: list[EquityQuote] = []
        self.read_calls = 0

    @property
    def identity(self) -> str:
        return "fixture-live-market"

    @property
    def capability_version(self) -> str:
        return self.version

    @property
    def capabilities(self) -> MarketDataCapabilities:
        return MarketDataCapabilities(replay=False)

    async def get_equity_quote(self, symbol: str, *, as_of: datetime | None = None) -> EquityQuote:
        self.read_calls += 1
        visible = [
            quote for quote in self.quotes if as_of is None or quote.metadata.observed_at <= as_of
        ]
        assert visible and all(quote.symbol == symbol for quote in visible)
        return visible[-1]

    async def get_bars(
        self,
        symbol: str,
        *,
        start: datetime | None = None,
        end: datetime | None = None,
        timeframe: str | None = None,
        as_of: datetime | None = None,
    ) -> Sequence[PriceBar]:
        return ()

    async def get_option_chain(
        self, symbol: str, *, as_of: datetime | None = None
    ) -> Sequence[OptionQuote]:
        return ()

    async def get_option_quote(
        self, instrument_id: str, *, as_of: datetime | None = None
    ) -> OptionQuote:
        raise AssertionError("queue cannot request order-specific quotes")

    async def scan_equities(
        self, request: EquityScanRequest, *, as_of: datetime | None = None
    ) -> Sequence[EquityQuote]:
        raise AssertionError("queue cannot create its own watchlist or scan")


class DurableResearcher:
    def __init__(
        self,
        repository: InMemoryAuditRepository,
        clock: VirtualClock,
        proposal: TradeProposal | None = None,
    ) -> None:
        self.repository, self.clock, self.proposal = repository, clock, proposal
        self.binding = repository.binding
        self.calls: list[UUID] = []
        self.work_count = 0

    async def on_event(
        self, event: SentinelEvent, market: MarketDataProvider
    ) -> TradeProposal | None:
        self.calls.append(event.event_id)
        key = f"{self.binding.idempotency_namespace}:{event.event_id}"
        existing = await self.repository.find_payload("candidate_runs", "event_key", key)
        if existing:
            saved = existing["payload"]["proposal"]
            return TradeProposal.model_validate(saved) if saved is not None else None
        self.work_count += 1
        result = self.proposal if event.event_type == "MARKET_MEASURED_CHANGE" else None
        await self.repository.append(
            "candidate_runs",
            {
                "created_at": self.clock.now(),
                "environment": self.binding.environment.value,
                "namespace": self.binding.idempotency_namespace,
                "event_key": key,
                "event_hash": sha256_json(event.model_dump(mode="json")),
                "status": "PROPOSED" if result else "WAIT",
                "proposal": result.model_dump(mode="json") if result else None,
            },
        )
        return result


@pytest.fixture
def shadow_binding(demo_binding: RuntimeBinding) -> RuntimeBinding:
    return demo_binding.model_copy(update={"demo_backend": DemoBackend.BROKER_SHADOW})


async def observation(
    producer: LiveReadSurveillanceWorker,
    market: FixtureMarket,
    clock: VirtualClock,
    *,
    price: str = "10",
) -> SentinelEvent:
    quote = EquityQuote(
        symbol="TEST",
        bid=Decimal(price) - Decimal("0.01"),
        ask=Decimal(price) + Decimal("0.01"),
        last=Decimal(price),
        volume=100,
        metadata=ProviderMetadata(
            provider=market.identity,
            capability_version=market.capability_version,
            observed_at=clock.now(),
            effective_at=clock.now(),
        ),
    )
    market.quotes.append(quote)
    event = await producer._persist(quote)
    assert event is not None
    return event


def components(
    binding: RuntimeBinding, instant: datetime
) -> tuple[
    InMemoryAuditRepository,
    VirtualClock,
    FixtureMarket,
    LiveReadSurveillanceWorker,
    DurableResearcher,
]:
    repository = InMemoryAuditRepository(binding)
    clock, market = VirtualClock(instant), FixtureMarket()
    producer = LiveReadSurveillanceWorker(market, clock, repository, watchlist=("TEST",))
    return repository, clock, market, producer, DurableResearcher(repository, clock)


async def test_no_event_is_quiet_and_makes_no_provider_or_research_calls(
    shadow_binding: RuntimeBinding,
    instant: datetime,
) -> None:
    repository, clock, market, _, researcher = components(shadow_binding, instant)
    assert await LiveMarketResearchQueue(repository, clock, market, researcher).tick() is None
    assert not researcher.calls and market.read_calls == 0
    assert await repository.list("system_runs") == []


async def test_baseline_then_measured_change_one_per_tick_and_restart(
    shadow_binding: RuntimeBinding,
    instant: datetime,
    proposal: TradeProposal,
) -> None:
    repository, clock, market, producer, researcher = components(shadow_binding, instant)
    researcher.proposal = proposal
    baseline = await observation(producer, market, clock)
    await clock.advance(timedelta(seconds=1))
    changed = await observation(producer, market, clock, price="11")
    queue = LiveMarketResearchQueue(repository, clock, market, researcher)
    assert await queue.tick() is None
    assert researcher.calls == [baseline.event_id]
    assert await queue.tick() == proposal
    assert researcher.calls == [baseline.event_id, changed.event_id]
    assert len(await repository.list("system_runs")) == 2
    restarted = LiveMarketResearchQueue(
        repository, clock, market, DurableResearcher(repository, clock)
    )
    assert await restarted.tick() is None
    assert len(await repository.list("system_runs")) == 2
    assert market.read_calls == 0  # Queue has neither a broker nor a provider-read path.


async def test_actual_candidate_worker_receives_baseline_and_reuses_existing_pipeline(
    instant: datetime,
) -> None:
    loaded = load_config()
    loaded = loaded.model_copy(
        update={
            "app": loaded.app.model_copy(
                update={
                    "execution_environment": ExecutionEnvironment.DEMO,
                    "demo_backend": DemoBackend.BROKER_SHADOW,
                }
            )
        }
    )
    repository, clock, market, producer, _ = components(loaded.bind_runtime(), instant)
    models = ScriptedReplayModelProvider(_scripted_outputs())
    researcher = CandidateResearchWorker(
        loaded,
        clock,
        repository,
        models,
        policy_profile=load_decision_policy_set().profiles["DEMO_EXPLORATORY"],
    )
    queue = LiveMarketResearchQueue(repository, clock, market, researcher)
    await observation(producer, market, clock)
    assert await queue.tick() is None
    baseline = await repository.find_payload(
        "candidate_features", "baseline_key", f"{repository.binding.idempotency_namespace}:TEST"
    )
    assert baseline is not None and baseline["payload"]["quote"]["last"] == "10"
    await clock.advance(timedelta(seconds=1))
    await observation(producer, market, clock, price="11")
    assert await queue.tick() is None  # Sparse fixture evidence cannot invent a qualified contract.
    assert len(await repository.list("candidate_runs")) == 4
    assert market.read_calls == 2
    assert models.calls == []


@pytest.mark.parametrize("after_commit", [False, True])
async def test_checkpoint_failure_retries_without_repeating_candidate_work(
    shadow_binding: RuntimeBinding,
    instant: datetime,
    after_commit: bool,
) -> None:
    class FailingRepository(InMemoryAuditRepository):
        failed = False

        async def append(self, table: str, value: BaseModel | Mapping[str, Any]) -> UUID:
            if table == "system_runs" and not self.failed:
                self.failed = True
                if after_commit:
                    await super().append(table, value)
                raise ConnectionError("fixture checkpoint response lost")
            return await super().append(table, value)

    repository = FailingRepository(shadow_binding)
    clock, market = VirtualClock(instant), FixtureMarket()
    producer = LiveReadSurveillanceWorker(market, clock, repository, watchlist=("TEST",))
    researcher = DurableResearcher(repository, clock)
    await observation(producer, market, clock)
    with pytest.raises(ConnectionError):
        await LiveMarketResearchQueue(repository, clock, market, researcher).tick()
    restarted = DurableResearcher(repository, clock)
    assert await LiveMarketResearchQueue(repository, clock, market, restarted).tick() is None
    assert researcher.work_count == 1 and restarted.work_count == 0
    assert len(await repository.list("candidate_runs")) == 1
    assert len(await repository.list("system_runs")) == 1


async def test_future_prefix_waits_instead_of_skipping_to_a_later_sequence(
    shadow_binding: RuntimeBinding,
    instant: datetime,
) -> None:
    repository, future_clock, market, producer, _ = components(
        shadow_binding, instant + timedelta(seconds=5)
    )
    event = await observation(producer, market, future_clock)
    clock = VirtualClock(instant)
    researcher = DurableResearcher(repository, clock)
    queue = LiveMarketResearchQueue(repository, clock, market, researcher)
    assert await queue.tick() is None
    assert not researcher.calls and await repository.list("system_runs") == []
    await clock.advance(timedelta(seconds=5))
    assert await queue.tick() is None
    assert researcher.calls == [event.event_id]


async def test_full_page_without_cursor_overlap_fails_without_skipping_history(
    shadow_binding: RuntimeBinding,
    instant: datetime,
) -> None:
    repository, clock, market, producer, researcher = components(shadow_binding, instant)
    for price in ("10", "11", "12"):
        await observation(producer, market, clock, price=price)
        await clock.advance(timedelta(seconds=1))
    with pytest.raises(SafetyCriticalError, match="pending prefix exceeds"):
        await LiveMarketResearchQueue(
            repository, clock, market, researcher, maximum_records=2
        ).tick()
    assert not researcher.calls and await repository.list("system_runs") == []


async def test_full_page_with_cursor_overlap_preserves_ascending_progress(
    shadow_binding: RuntimeBinding,
    instant: datetime,
) -> None:
    repository, clock, market, producer, researcher = components(shadow_binding, instant)
    baseline = await observation(producer, market, clock)
    queue = LiveMarketResearchQueue(repository, clock, market, researcher, maximum_records=2)
    assert await queue.tick() is None
    await clock.advance(timedelta(seconds=1))
    changed = await observation(producer, market, clock, price="11")
    assert await queue.tick() is None
    assert researcher.calls == [baseline.event_id, changed.event_id]


@pytest.mark.parametrize(
    "fault", ["snapshot_missing", "provider", "version", "snapshot_tamper", "namespace"]
)
async def test_event_provenance_is_verified_before_research_or_checkpoint(
    shadow_binding: RuntimeBinding,
    instant: datetime,
    fault: str,
) -> None:
    repository, clock, market, producer, researcher = components(shadow_binding, instant)
    await observation(producer, market, clock)
    if fault == "snapshot_missing":
        repository._rows["market_snapshots"].clear()
    elif fault == "snapshot_tamper":
        repository._rows["market_snapshots"][0]["payload"]["quote"]["last"] = "999"
    elif fault == "namespace":
        repository._rows["sentinel_events"][0]["payload"]["namespace"] = "different-namespace"
    else:
        key = "provider" if fault == "provider" else "capability_version"
        repository._rows["sentinel_events"][0]["payload"]["payload"][key] = "other"
    with pytest.raises((SafetyCriticalError, DataInvalidError)):
        await LiveMarketResearchQueue(repository, clock, market, researcher).tick()
    assert not researcher.calls and await repository.list("system_runs") == []


async def test_binding_or_provider_drift_disables_queue(
    shadow_binding: RuntimeBinding,
    instant: datetime,
) -> None:
    repository, clock, market, _, researcher = components(shadow_binding, instant)
    queue = LiveMarketResearchQueue(repository, clock, market, researcher)
    market.version = "changed-v2"
    with pytest.raises(SafetyCriticalError, match="identity/capabilities"):
        await queue.tick()
    market.version = "fixture-live-v1"
    repository.binding = shadow_binding.model_copy(
        update={"environment": ExecutionEnvironment.LIVE}
    )
    with pytest.raises(SafetyCriticalError, match="BROKER_SHADOW"):
        await queue.tick()


async def test_queue_requires_terminal_durable_candidate_before_cursor(
    shadow_binding: RuntimeBinding,
    instant: datetime,
) -> None:
    repository, clock, market, producer, researcher = components(shadow_binding, instant)
    await observation(producer, market, clock)

    async def undurable(event: SentinelEvent, provider: MarketDataProvider) -> None:
        return None

    researcher.on_event = undurable  # type: ignore[method-assign]
    with pytest.raises(SafetyCriticalError, match="must be durable"):
        await LiveMarketResearchQueue(repository, clock, market, researcher).tick()
    assert await repository.list("system_runs") == []


async def test_cursor_rejects_modified_event_or_future_checkpoint(
    shadow_binding: RuntimeBinding,
    instant: datetime,
) -> None:
    repository, clock, market, producer, researcher = components(shadow_binding, instant)
    await observation(producer, market, clock)
    queue = LiveMarketResearchQueue(repository, clock, market, researcher)
    await queue.tick()
    checkpoint = repository._rows["system_runs"][0]
    checkpoint["payload"]["created_at"] = (instant + timedelta(seconds=1)).isoformat()
    with pytest.raises(SafetyCriticalError, match="checkpoint provenance or clock"):
        await queue.tick()
    checkpoint["payload"]["created_at"] = instant.isoformat()
    repository._rows["sentinel_events"][0]["payload"]["raw_reference_ids"] = [str(UUID(int=1))]
    with pytest.raises(SafetyCriticalError, match="event content changed"):
        await queue.tick()


async def test_concurrent_ticks_are_serialized_and_timeout_leaves_cursor_pending(
    shadow_binding: RuntimeBinding,
    instant: datetime,
) -> None:
    repository, clock, market, producer, researcher = components(shadow_binding, instant)
    await observation(producer, market, clock)
    queue = LiveMarketResearchQueue(repository, clock, market, researcher)
    assert await asyncio.gather(queue.tick(), queue.tick()) == [None, None]
    assert researcher.work_count == 1
    await clock.advance(timedelta(seconds=1))
    await observation(producer, market, clock, price="11")

    async def slow(event: SentinelEvent, provider: MarketDataProvider) -> None:
        await asyncio.sleep(1)

    researcher.on_event = slow  # type: ignore[method-assign]
    with pytest.raises(TimeoutError):
        await LiveMarketResearchQueue(
            repository, clock, market, researcher, maximum_seconds=0.01
        ).tick()
    assert len(await repository.list("system_runs")) == 1


@pytest.mark.parametrize(
    "bounds",
    [
        {"maximum_records": True},
        {"maximum_records": 1001},
        {"maximum_records": 0},
        {"maximum_seconds": True},
        {"maximum_seconds": float("nan")},
        {"maximum_seconds": float("inf")},
        {"maximum_seconds": 201},
    ],
)
def test_limits_are_finite_and_bounded(
    shadow_binding: RuntimeBinding,
    instant: datetime,
    bounds: dict[str, Any],
) -> None:
    repository, clock, market, _, researcher = components(shadow_binding, instant)
    with pytest.raises(ValueError):
        LiveMarketResearchQueue(repository, clock, market, researcher, **bounds)
