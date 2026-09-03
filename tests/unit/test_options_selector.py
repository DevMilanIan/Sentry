from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest

from app.clock.base import VirtualClock
from app.domain.enums import Direction, SelectorStatus
from app.market.fixtures import bundled_fixture_path
from app.market.replay import OfflineReplayMarketDataProvider
from app.options.selector import ContractSelector, ContractSelectorConfig


@pytest.mark.asyncio
async def test_selector_filters_direction_liquidity_and_affordability() -> None:
    clock = VirtualClock(datetime(2026, 1, 5, 14, 31, tzinfo=UTC))
    provider = OfflineReplayMarketDataProvider.from_path(bundled_fixture_path(), clock)
    quotes = await provider.get_option_chain("ACME")
    selector = ContractSelector(
        ContractSelectorConfig(
            maximum_contract_cost=Decimal("25"),
            minimum_open_interest=100,
            minimum_option_volume=10,
            maximum_bid_ask_percent=Decimal("30"),
        ),
        clock,
    )

    result = selector.evaluate(Direction.BULLISH, Decimal("100"), quotes)
    assert result.selection.status is SelectorStatus.CONTRACT_FOUND
    assert [quote.contract.instrument_id for quote in result.selection.ranked_quotes] == [
        "ACME-20260123-C-102"
    ]
    assert "spread_too_wide" in result.selection.rejected_reasons["ACME-20260123-C-110"]
    assert "direction_mismatch" in result.selection.rejected_reasons["ACME-20260123-P-98"]


@pytest.mark.asyncio
async def test_selector_distinguishes_no_contract() -> None:
    clock = VirtualClock(datetime(2026, 1, 5, 14, 31, tzinfo=UTC))
    provider = OfflineReplayMarketDataProvider.from_path(bundled_fixture_path(), clock)
    selector = ContractSelector(
        ContractSelectorConfig(maximum_contract_cost=Decimal("5")),
        clock,
    )
    selection = selector.select(
        Direction.BEARISH,
        Decimal("100"),
        await provider.get_option_chain("ACME"),
    )
    assert selection.status is SelectorStatus.NO_CONTRACT
    assert selection.ranked_quotes == ()
