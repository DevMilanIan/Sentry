from __future__ import annotations

import hashlib
from abc import ABC, abstractmethod
from dataclasses import dataclass
from decimal import ROUND_FLOOR, Decimal

from app.domain.enums import OrderSide
from app.domain.models import BrokerOrder, OptionQuote


@dataclass(frozen=True, slots=True)
class FillDecision:
    quantity: int
    price: Decimal | None
    deterministic_seed: int
    reason: str

    @property
    def should_fill(self) -> bool:
        return self.quantity > 0 and self.price is not None


class FillModel(ABC):
    """Pure, versioned limit-fill decision over one post-order quote event."""

    version: str

    @abstractmethod
    def evaluate(
        self,
        *,
        order: BrokerOrder,
        quote: OptionQuote,
        remaining_quantity: int,
    ) -> FillDecision:
        raise NotImplementedError

    @staticmethod
    def seed_for(order: BrokerOrder, quote: OptionQuote, version: str) -> int:
        material = f"{order.order_id}:{quote.snapshot_id}:{version}".encode()
        return int.from_bytes(hashlib.sha256(material).digest()[:8], "big", signed=False)


class OptimisticFillModel(FillModel):
    """Testing model: a displayed price or last trade touching the limit fills fully."""

    version = "optimistic-limit-v1"

    def evaluate(
        self,
        *,
        order: BrokerOrder,
        quote: OptionQuote,
        remaining_quantity: int,
    ) -> FillDecision:
        seed = self.seed_for(order, quote, self.version)
        if remaining_quantity <= 0:
            return FillDecision(0, None, seed, "order has no remaining quantity")

        if order.side is OrderSide.BUY_TO_OPEN:
            displayed_touch = quote.ask <= order.limit_price and quote.ask > 0
            last_touch = quote.last is not None and quote.last <= order.limit_price
            if not displayed_touch and not last_touch:
                return FillDecision(0, None, seed, "buy limit was not touched")
            price = min(order.limit_price, quote.ask) if displayed_touch else order.limit_price
        else:
            displayed_touch = quote.bid >= order.limit_price and quote.bid > 0
            last_touch = quote.last is not None and quote.last >= order.limit_price
            if not displayed_touch and not last_touch:
                return FillDecision(0, None, seed, "sell limit was not touched")
            price = max(order.limit_price, quote.bid) if displayed_touch else order.limit_price

        return FillDecision(
            remaining_quantity,
            price,
            seed,
            "optimistic model filled the complete remaining quantity on touch",
        )


class ConservativeFillModel(FillModel):
    """Queue-aware deterministic model that does not assume every touch fills.

    A price strictly through the limit is fillable.  At the limit, a stable hash
    models uncertain queue priority.  Displayed size and a participation cap can
    produce partial fills across subsequent quote events.
    """

    version = "conservative-queue-v1"

    def __init__(
        self,
        *,
        touch_fill_probability: Decimal = Decimal("0.30"),
        displayed_size_participation: Decimal = Decimal("0.50"),
        seed_salt: int = 0,
    ) -> None:
        if not Decimal("0") <= touch_fill_probability <= Decimal("1"):
            raise ValueError("touch_fill_probability must be between zero and one")
        if not Decimal("0") < displayed_size_participation <= Decimal("1"):
            raise ValueError("displayed_size_participation must be in (0, 1]")
        self.touch_fill_probability = touch_fill_probability
        self.displayed_size_participation = displayed_size_participation
        self.seed_salt = seed_salt
        self.version = (
            "conservative-queue-v1"
            f";touch={touch_fill_probability};participation={displayed_size_participation}"
            f";seed={seed_salt}"
        )

    def evaluate(
        self,
        *,
        order: BrokerOrder,
        quote: OptionQuote,
        remaining_quantity: int,
    ) -> FillDecision:
        seed = self.seed_for(order, quote, self.version)
        if remaining_quantity <= 0:
            return FillDecision(0, None, seed, "order has no remaining quantity")

        if order.side is OrderSide.BUY_TO_OPEN:
            executable_price = quote.ask
            displayed_size = quote.ask_size
            improved = executable_price > 0 and executable_price < order.limit_price
            touched = executable_price > 0 and executable_price == order.limit_price
        else:
            executable_price = quote.bid
            displayed_size = quote.bid_size
            improved = executable_price > order.limit_price
            touched = executable_price > 0 and executable_price == order.limit_price

        if not improved and not touched:
            return FillDecision(0, None, seed, "displayed market did not reach the limit")

        if displayed_size is not None and displayed_size <= 0:
            return FillDecision(0, None, seed, "displayed executable size was zero")

        if touched:
            sample = Decimal(seed % 1_000_000) / Decimal(1_000_000)
            if sample >= self.touch_fill_probability:
                return FillDecision(
                    0,
                    None,
                    seed,
                    "limit touched but deterministic queue gate missed",
                )

        if displayed_size is None:
            # Unknown liquidity is deliberately capped to one contract.
            fill_quantity = min(remaining_quantity, 1)
        else:
            participated = int(
                (Decimal(displayed_size) * self.displayed_size_participation).to_integral_value(
                    rounding=ROUND_FLOOR
                )
            )
            fill_quantity = min(remaining_quantity, max(1, participated))

        reason = "market traded through limit" if improved else "queue gate accepted limit touch"
        return FillDecision(fill_quantity, executable_price, seed, reason)
