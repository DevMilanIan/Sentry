from __future__ import annotations

import asyncio
from collections.abc import Mapping
from datetime import timedelta
from decimal import Decimal
from typing import Any
from uuid import UUID, uuid4

import pytest
from pydantic import BaseModel

from app.clock.base import VirtualClock
from app.config import RuntimeBinding
from app.db.repository import InMemoryAuditRepository
from app.domain.enums import DemoBackend, OptionType
from app.domain.models import EquityQuote, OptionContract, OptionQuote, ProviderMetadata
from app.exceptions import DataInvalidError, SafetyCriticalError, TransientError
from app.market.models import MarketDataCapabilities, ReplayFixture, ReplayRecord
from app.market.replay import OfflineReplayMarketDataProvider
from app.sentinel.live_reads import LiveReadLimits, LiveReadSurveillanceWorker


class SyntheticCurrentProvider(OfflineReplayMarketDataProvider):
    """Synthetic causal fixture exercising a current-read capability contract, not live evidence."""

    @property
    def capabilities(self) -> MarketDataCapabilities:
        return MarketDataCapabilities(replay=False)


def market(clock: VirtualClock) -> SyntheticCurrentProvider:
    records: list[ReplayRecord] = []
    for seconds, price in ((0, "10"), (5, "11"), (10, "12")):
        instant = clock.now() + timedelta(seconds=seconds)
        metadata = ProviderMetadata(
            provider="synthetic-current-test",
            capability_version="test-v1",
            observed_at=instant,
            effective_at=instant,
            source_id=f"test-{seconds}",
        )
        equity = EquityQuote(
            symbol="TEST",
            bid=Decimal(price),
            ask=Decimal(price) + Decimal("0.01"),
            last=Decimal(price),
            volume=100 + seconds,
            metadata=metadata,
        )
        option = OptionQuote(
            contract=OptionContract(
                instrument_id="test-call",
                symbol="TEST",
                option_type=OptionType.CALL,
                strike=Decimal("10"),
                expiration=instant.date() + timedelta(days=30),
            ),
            bid=Decimal("0.05") + Decimal(seconds) / 100,
            ask=Decimal("0.15"),
            last=Decimal("0.10"),
            volume=50 + seconds,
            open_interest=500,
            metadata=metadata,
        )
        for kind, quote in (("equity_quote", equity), ("option_quote", option)):
            records.append(
                ReplayRecord(
                    sequence=len(records),
                    kind=kind,
                    observed_at=instant,
                    effective_at=instant,
                    payload=quote.model_dump(mode="json"),
                )
            )
    return SyntheticCurrentProvider(
        ReplayFixture(
            version="test-v1",
            provider="synthetic-current-test",
            capability_version="test-v1",
            records=tuple(records),
        ),
        clock,
    )


def repository(binding: RuntimeBinding) -> InMemoryAuditRepository:
    return InMemoryAuditRepository(
        binding.model_copy(update={"demo_backend": DemoBackend.BROKER_SHADOW})
    )


def worker(
    provider: SyntheticCurrentProvider,
    clock: VirtualClock,
    audit: InMemoryAuditRepository,
    **kwargs: Any,
) -> LiveReadSurveillanceWorker:
    return LiveReadSurveillanceWorker(provider, clock, audit, watchlist=("TEST",), **kwargs)


async def test_baselines_then_measured_changes_and_durable_snapshot_event_order(
    clock: VirtualClock,
    demo_binding: RuntimeBinding,
) -> None:
    audit = repository(demo_binding)
    sentinel = worker(market(clock), clock, audit)
    assert not await sentinel.health()
    baselines = await sentinel.tick()
    assert len(baselines) == 2
    assert all(event.event_type == "MARKET_BASELINE" for event in baselines)
    assert all(event.severity == 0 and event.payload["changes"] == {} for event in baselines)
    assert await sentinel.health()
    assert await sentinel.scan() == ()
    await clock.advance(timedelta(seconds=5))
    changes = await sentinel.tick()
    assert len(changes) == 2
    assert all(event.event_type == "MARKET_MEASURED_CHANGE" for event in changes)
    equity = next(event for event in changes if event.payload["quote_kind"] == "equity")
    assert equity.payload["changes"]["last"] == {
        "previous": "10",
        "current": "11",
        "delta": "1",
    }
    assert equity.raw_reference_ids[0] == baselines[0].raw_reference_ids[0]
    snapshots = await audit.list("market_snapshots")
    assert len(snapshots) == 2 and len(await audit.list("option_snapshots")) == 2
    assert snapshots[1]["payload"]["quote"]["metadata"]["effective_at"] == (
        clock.now().isoformat().replace("+00:00", "Z")
    )
    assert all(row["payload"]["data_mode"] == "LIVE_READ" for row in snapshots)
    assert set(audit._rows) <= {
        "market_snapshots",
        "option_snapshots",
        "sentinel_events",
        "health_events",
    }


async def test_health_expires_between_poll_intervals_and_failed_scan_latches_unhealthy(
    clock: VirtualClock,
    demo_binding: RuntimeBinding,
) -> None:
    audit = repository(demo_binding)
    sentinel = worker(market(clock), clock, audit)
    await sentinel.tick()
    await clock.advance(timedelta(seconds=121))
    assert not await sentinel.health()
    assert not sentinel.last_scan_healthy
    await sentinel.tick()  # Latest fixture quote is only 111 seconds old.
    assert await sentinel.health()
    audit.writable = False
    with pytest.raises(SafetyCriticalError):
        await sentinel.tick()
    audit.writable = True
    assert not await sentinel.health()
    await sentinel.tick()
    assert await sentinel.health()


def test_offline_and_replay_providers_cannot_be_relabelled_as_current(
    clock: VirtualClock,
    demo_binding: RuntimeBinding,
) -> None:
    provider = market(clock)
    with pytest.raises(SafetyCriticalError):
        worker(provider, clock, InMemoryAuditRepository(demo_binding))
    replay = OfflineReplayMarketDataProvider(provider._fixture, clock)
    with pytest.raises(SafetyCriticalError):
        LiveReadSurveillanceWorker(replay, clock, repository(demo_binding), watchlist=("TEST",))


@pytest.mark.parametrize("watchlist", [(), ("TEST", "test"), ("../../secrets",), "TEST"])
def test_watchlist_is_explicit_and_unambiguous(
    clock: VirtualClock,
    demo_binding: RuntimeBinding,
    watchlist: Any,
) -> None:
    with pytest.raises(ValueError):
        LiveReadSurveillanceWorker(
            market(clock), clock, repository(demo_binding), watchlist=watchlist
        )


@pytest.mark.parametrize("change", ["future", "stale", "provider", "version", "early", "symbol"])
async def test_bad_quote_evidence_never_persists_market_snapshots(
    clock: VirtualClock,
    demo_binding: RuntimeBinding,
    monkeypatch: pytest.MonkeyPatch,
    change: str,
) -> None:
    provider, audit = market(clock), repository(demo_binding)
    original = await provider.get_equity_quote("TEST")
    metadata: dict[str, Any] = {}
    if change == "future":
        metadata["observed_at"] = clock.now() + timedelta(seconds=1)
    elif change == "stale":
        metadata.update(
            observed_at=clock.now() - timedelta(seconds=121),
            effective_at=clock.now() - timedelta(seconds=121),
        )
    elif change == "early":
        metadata["observed_at"] = clock.now() - timedelta(seconds=1)
    else:
        metadata[
            {"provider": "provider", "version": "capability_version"}.get(change, "source_id")
        ] = "unrelated"
    altered = original.model_copy(
        update={"metadata": original.metadata.model_copy(update=metadata)}
    )
    if change == "symbol":
        altered = altered.model_copy(update={"symbol": "OTHER"})

    async def wrong_quote(*_args: Any, **_kwargs: Any) -> EquityQuote:
        return altered

    monkeypatch.setattr(provider, "get_equity_quote", wrong_quote)
    sentinel = worker(provider, clock, audit)
    with pytest.raises(DataInvalidError):
        await sentinel.tick()
    assert not await audit.list("market_snapshots")
    assert not await audit.list("sentinel_events")
    assert not await sentinel.health()
    assert (await audit.list("health_events"))[-1]["payload"]["healthy"] is False


@pytest.mark.parametrize("mode", ["empty", "over_budget", "duplicate", "wrong_symbol"])
async def test_option_chain_coverage_and_bounds_are_enforced_before_market_writes(
    clock: VirtualClock,
    demo_binding: RuntimeBinding,
    monkeypatch: pytest.MonkeyPatch,
    mode: str,
) -> None:
    provider, audit = market(clock), repository(demo_binding)
    quote = (await provider.get_option_chain("TEST"))[0]

    async def chain(*_args: Any, **_kwargs: Any) -> tuple[OptionQuote, ...]:
        if mode == "empty":
            return ()
        if mode == "wrong_symbol":
            return (
                quote.model_copy(
                    update={
                        "contract": quote.contract.model_copy(update={"symbol": "OTHER"}),
                    }
                ),
            )
        return (quote, quote)

    monkeypatch.setattr(provider, "get_option_chain", chain)
    limits = LiveReadLimits(maximum_options_per_symbol=1 if mode == "over_budget" else 2)
    with pytest.raises(DataInvalidError):
        await worker(provider, clock, audit, limits=limits).tick()
    assert not await audit.list("market_snapshots")
    assert not await audit.list("option_snapshots")


async def test_changed_adapter_ids_do_not_create_duplicate_observations(
    clock: VirtualClock,
    demo_binding: RuntimeBinding,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider, audit = market(clock), repository(demo_binding)
    original = provider.get_equity_quote

    async def quote(*args: Any, **kwargs: Any) -> EquityQuote:
        return (await original(*args, **kwargs)).model_copy(update={"snapshot_id": uuid4()})

    monkeypatch.setattr(provider, "get_equity_quote", quote)
    sentinel = worker(provider, clock, audit)
    await sentinel.tick()
    assert await sentinel.tick() == ()
    assert len(await audit.list("market_snapshots")) == 1


class InterruptedRepository(InMemoryAuditRepository):
    failure_table = "sentinel_events"
    after_commit = False
    failed = False

    async def append(self, table: str, value: BaseModel | Mapping[str, Any]) -> UUID:
        if table == self.failure_table and not self.failed:
            self.failed = True
            if self.after_commit:
                await super().append(table, value)
            raise ConnectionError("sensitive-provider-test-text")
        return await super().append(table, value)


@pytest.mark.parametrize(
    "table,after_commit",
    [
        ("sentinel_events", False),
        ("sentinel_events", True),
        ("market_snapshots", True),
    ],
)
async def test_restart_repairs_snapshot_event_gap_even_after_provider_rolls_forward(
    clock: VirtualClock,
    demo_binding: RuntimeBinding,
    table: str,
    after_commit: bool,
) -> None:
    audit = InterruptedRepository(repository(demo_binding).binding)
    audit.failure_table, audit.after_commit = table, after_commit
    provider = market(clock)
    with pytest.raises(TransientError) as failure:
        await worker(provider, clock, audit).tick()
    assert "sensitive-provider-test-text" not in str(failure.value)
    first = (await audit.list("market_snapshots"))[0]["payload"]
    await clock.advance(timedelta(seconds=5))
    restored = worker(provider, clock, audit)
    events = await restored.tick()
    assert first["event"]["event_id"] in {str(event.event_id) for event in events}
    rows = await audit.list("sentinel_events")
    keys = [row["payload"]["deduplication_key"] for row in rows]
    assert len(keys) == len(set(keys))
    assert len(await audit.list("market_snapshots")) == 2
    assert await restored.health()


async def test_restart_restores_baselines_without_fabricating_new_anomaly(
    clock: VirtualClock,
    demo_binding: RuntimeBinding,
) -> None:
    audit, provider = repository(demo_binding), market(clock)
    first = await worker(provider, clock, audit).tick()
    restored = worker(provider, clock, audit)
    repeated = await restored.tick()
    assert {event.event_id for event in repeated} == {event.event_id for event in first}
    assert len(await audit.list("sentinel_events")) == 2
    await clock.advance(timedelta(seconds=5))
    assert all(event.event_type == "MARKET_MEASURED_CHANGE" for event in await restored.tick())


async def test_wall_deadline_and_provider_failure_do_not_leak_exception_content(
    clock: VirtualClock,
    demo_binding: RuntimeBinding,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider, audit = market(clock), repository(demo_binding)

    async def slow(*_args: Any, **_kwargs: Any) -> EquityQuote:
        await asyncio.sleep(1)
        raise AssertionError("must time out first")

    monkeypatch.setattr(provider, "get_equity_quote", slow)
    sentinel = worker(provider, clock, audit, limits=LiveReadLimits(tick_timeout_seconds=0.01))
    async with asyncio.timeout(0.5):
        with pytest.raises(TransientError):
            await sentinel.tick()
    assert not await sentinel.health()
    assert not await audit.list("market_snapshots")


async def test_missing_read_capability_and_capability_drift_fail_closed(
    clock: VirtualClock,
    demo_binding: RuntimeBinding,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider, audit = market(clock), repository(demo_binding)
    sentinel = worker(provider, clock, audit)
    await sentinel.tick()
    monkeypatch.setattr(
        SyntheticCurrentProvider,
        "capabilities",
        property(lambda _self: MarketDataCapabilities(option_chains=False)),
    )
    assert not await sentinel.health()
    with pytest.raises(SafetyCriticalError):
        await sentinel.tick()
    with pytest.raises(SafetyCriticalError):
        worker(provider, clock, audit)


async def test_already_seen_old_quote_cannot_bypass_stream_regression_guard(
    clock: VirtualClock,
    demo_binding: RuntimeBinding,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider, audit = market(clock), repository(demo_binding)
    prior = await provider.get_equity_quote("TEST")
    sentinel = worker(provider, clock, audit)
    await sentinel.tick()
    await clock.advance(timedelta(seconds=5))
    await sentinel.tick()

    async def old_quote(*_args: Any, **_kwargs: Any) -> EquityQuote:
        return prior

    monkeypatch.setattr(provider, "get_equity_quote", old_quote)
    with pytest.raises(DataInvalidError):
        await sentinel.tick()
    assert not await sentinel.health()
    assert len(await audit.list("market_snapshots")) == 2
