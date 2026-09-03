from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Sequence
from datetime import datetime

from app.domain.models import EquityQuote, OptionQuote
from app.market.models import EquityScanRequest, MarketDataCapabilities, PriceBar


class MarketDataProvider(ABC):
    """Broad-market read boundary, deliberately separate from broker authority."""

    @property
    @abstractmethod
    def identity(self) -> str:
        """Stable provider identity recorded with snapshots."""

    @property
    @abstractmethod
    def capability_version(self) -> str:
        """Version of the adapter/provider contract."""

    @property
    @abstractmethod
    def capabilities(self) -> MarketDataCapabilities:
        """Advertise supported read-only capabilities."""

    @abstractmethod
    async def get_equity_quote(self, symbol: str, *, as_of: datetime | None = None) -> EquityQuote:
        """Return the latest causally available quote at ``as_of``."""

    @abstractmethod
    async def get_bars(
        self,
        symbol: str,
        *,
        start: datetime | None = None,
        end: datetime | None = None,
        timeframe: str | None = None,
        as_of: datetime | None = None,
    ) -> Sequence[PriceBar]:
        """Return complete bars visible at ``as_of``."""

    @abstractmethod
    async def get_option_chain(
        self, symbol: str, *, as_of: datetime | None = None
    ) -> Sequence[OptionQuote]:
        """Return the latest quote per option instrument visible at ``as_of``."""

    @abstractmethod
    async def get_option_quote(
        self, instrument_id: str, *, as_of: datetime | None = None
    ) -> OptionQuote:
        """Return an exact instrument quote visible at ``as_of``."""

    @abstractmethod
    async def scan_equities(
        self, request: EquityScanRequest, *, as_of: datetime | None = None
    ) -> Sequence[EquityQuote]:
        """Deterministically scan the current visible quote universe."""

    async def close(self) -> None:
        """Release optional adapter resources."""

        return None
