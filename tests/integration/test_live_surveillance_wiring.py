from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime, timedelta
from decimal import Decimal

import pytest

from app.clock.base import VirtualClock
from app.config import load_config
from app.db.repository import InMemoryAuditRepository
from app.demo.offline_scenario import _scripted_outputs
from app.domain.enums import DemoBackend
from app.domain.models import EquityQuote, OptionQuote, ProviderMetadata, TradeProposal
from app.market.base import MarketDataProvider
from app.market.models import EquityScanRequest, MarketDataCapabilities, PriceBar
from app.reasoning.scripted import ScriptedReplayModelProvider
from app.runtime import build_application


class SyntheticCurrentProvider(MarketDataProvider):
    """Synthetic current-read contract, never a replay qualification claim."""

    identity = "synthetic-current-read"
    capability_version = "test-v1"
    capabilities = MarketDataCapabilities()

    def __init__(self, proposal: TradeProposal, instant: datetime) -> None:
        metadata = ProviderMetadata(
            provider=self.identity,
            capability_version=self.capability_version,
            observed_at=instant,
            effective_at=instant,
        )
        self.equity = EquityQuote(
            symbol="TEST",
            bid=Decimal("10"),
            ask=Decimal("10.01"),
            last=Decimal("10"),
            volume=1000,
            metadata=metadata,
        )
        self.option = OptionQuote(
            contract=proposal.contract,
            bid=Decimal("0.07"),
            ask=Decimal("0.08"),
            metadata=metadata,
        )
        self.reads = 0
        self.closed = False

    async def get_equity_quote(self, symbol: str, *, as_of: datetime | None = None) -> EquityQuote:
        assert symbol == "TEST"
        self.reads += 1
        return self.equity

    async def get_option_chain(
        self, symbol: str, *, as_of: datetime | None = None
    ) -> Sequence[OptionQuote]:
        assert symbol == "TEST"
        return (self.option,)

    async def get_option_quote(
        self, instrument_id: str, *, as_of: datetime | None = None
    ) -> OptionQuote:
        assert instrument_id == self.option.contract.instrument_id
        return self.option

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

    async def scan_equities(
        self, request: EquityScanRequest, *, as_of: datetime | None = None
    ) -> Sequence[EquityQuote]:
        return (self.equity,)

    async def close(self) -> None:
        self.closed = True


async def test_composed_current_surveillance_expires_without_rescan_and_never_enables_broker(
    proposal: TradeProposal,
    instant: datetime,
) -> None:
    loaded = load_config()
    loaded = loaded.model_copy(
        update={"app": loaded.app.model_copy(update={"demo_backend": DemoBackend.BROKER_SHADOW})}
    )
    repository = InMemoryAuditRepository(loaded.bind_runtime())
    market = SyntheticCurrentProvider(proposal, instant)
    clock = VirtualClock(instant)
    runtime = await build_application(
        loaded,
        repository=repository,
        wall_clock=clock,
        model_provider=ScriptedReplayModelProvider(_scripted_outputs()),
        market_provider=market,
        market_watchlist=("TEST",),
    )
    try:
        jobs = {job.name: job for job in runtime.controller._jobs}
        assert "official_sources" in jobs
        assert "closed_position_review" in jobs
        assert "offline_replay" not in jobs
        await jobs["current_market_surveillance"].callback()
        assert runtime.view.market_data_fresh
        events = await repository.list_payloads("sentinel_events")
        assert events and all(row["payload"]["event_type"] == "MARKET_BASELINE" for row in events)
        await jobs["market_event_research"].callback()
        attempts = await repository.list_payloads("candidate_runs")
        assert attempts[0]["payload"]["status"] == "WAIT"
        assert not runtime.view.proposals
        reads_after_research = market.reads
        await runtime.controller.health_once()
        assert not runtime.view.broker_connected
        assert not runtime.view.safety.permits_new_entry()
        await clock.advance(timedelta(seconds=121))
        await runtime.controller.health_once()
        assert not runtime.view.market_data_fresh
        assert market.reads == reads_after_research
        assert runtime.broker is None
        assert runtime.view.write_firewall == "DENY_ALL_WRITES"
    finally:
        await runtime.close()
    assert market.closed


async def test_offline_binding_rejects_current_provider_before_any_read(
    proposal: TradeProposal,
    instant: datetime,
) -> None:
    market = SyntheticCurrentProvider(proposal, instant)
    with pytest.raises(ValueError, match="BROKER_SHADOW"):
        await build_application(load_config(), market_provider=market, market_watchlist=("TEST",))
    assert market.reads == 0
