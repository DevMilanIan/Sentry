from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from app.clock.base import VirtualClock
from app.market.fixtures import bundled_fixture_path
from app.market.models import EquityScanRequest
from app.market.replay import (
    LookaheadViolationError,
    OfflineReplayMarketDataProvider,
)


@pytest.mark.asyncio
async def test_replay_provider_never_exposes_future_records() -> None:
    clock = VirtualClock(datetime(2026, 1, 5, 14, 30, tzinfo=UTC))
    provider = OfflineReplayMarketDataProvider.from_path(
        bundled_fixture_path(),
        clock,
    )

    first = await provider.get_equity_quote("acme")
    assert first.last == Decimal("100.00")
    assert await provider.get_option_chain("ACME") == ()
    with pytest.raises(LookaheadViolationError):
        await provider.get_equity_quote(
            "ACME",
            as_of=clock.now() + timedelta(seconds=1),
        )

    await clock.advance(timedelta(minutes=5))
    current = await provider.get_equity_quote("ACME")
    assert current.last == Decimal("100.90")
    assert current.snapshot_id != first.snapshot_id
    assert len(await provider.get_bars("ACME", timeframe="5m")) == 1


@pytest.mark.asyncio
async def test_replay_chain_and_scan_are_deterministic() -> None:
    clock = VirtualClock(datetime(2026, 1, 5, 14, 31, tzinfo=UTC))
    provider = OfflineReplayMarketDataProvider.from_path(bundled_fixture_path(), clock)

    chain = await provider.get_option_chain("ACME")
    assert [quote.contract.instrument_id for quote in chain] == [
        "ACME-20260123-C-102",
        "ACME-20260123-C-110",
        "ACME-20260123-P-98",
    ]
    exact = await provider.get_option_quote("ACME-20260123-C-102")
    assert exact.ask == Decimal("0.20")
    scan = await provider.scan_equities(
        EquityScanRequest(minimum_price=Decimal("50"), minimum_volume=10)
    )
    assert [quote.symbol for quote in scan] == ["ACME"]
    assert provider.next_available_at() == datetime(2026, 1, 5, 14, 35, tzinfo=UTC)
