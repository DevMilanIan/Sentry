from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, time, timedelta
from decimal import Decimal
from enum import StrEnum
from zoneinfo import ZoneInfo

from app.clock.base import Clock
from app.clock.market_calendar import UsEquityCalendar
from app.domain.enums import OrderSide
from app.domain.models import OptionQuote, Position


class ExitTrigger(StrEnum):
    PROFIT_TARGET = "profit_target"
    LOSS_THRESHOLD = "loss_threshold"
    THESIS_INVALIDATED = "thesis_invalidated"
    CATALYST_FAILED = "catalyst_failed"
    DTE_CUTOFF = "dte_cutoff"
    END_OF_DAY = "end_of_day"
    LIQUIDITY_DETERIORATION = "liquidity_deterioration"
    HARD_PORTFOLIO_EMERGENCY = "hard_portfolio_emergency"


@dataclass(frozen=True, slots=True)
class ExitPolicy:
    version: str
    profit_target_fraction: Decimal = Decimal("0.50")
    loss_threshold_fraction: Decimal = Decimal("0.35")
    sell_to_close_days_before_expiration: int = 1
    maximum_bid_ask_percent: Decimal = Decimal("40")
    end_of_day_exit: bool = False
    end_of_day_cutoff: time = time(15, 45)
    market_timezone: str = "America/New_York"
    maximum_quote_age: timedelta = timedelta(seconds=120)

    def __post_init__(self) -> None:
        if not self.version:
            raise ValueError("exit policy version is required")
        if self.profit_target_fraction < 0 or self.loss_threshold_fraction < 0:
            raise ValueError("P&L thresholds cannot be negative")
        if self.sell_to_close_days_before_expiration < 0:
            raise ValueError("expiration cutoff cannot be negative")
        if self.maximum_bid_ask_percent <= 0 or self.maximum_quote_age.total_seconds() <= 0:
            raise ValueError("liquidity/freshness thresholds must be positive")
        ZoneInfo(self.market_timezone)


@dataclass(frozen=True, slots=True)
class ExitDecision:
    position_id: object
    evaluated_at: datetime
    should_exit: bool
    executable: bool
    side: OrderSide
    quantity: int
    limit_price: Decimal | None
    triggers: tuple[ExitTrigger, ...]
    unrealized_pnl: Decimal
    unrealized_return: Decimal
    quote_snapshot_id: object
    policy_version: str
    reason: str


class PositionManager:
    """Evaluates predeclared exits without model inference or wall-clock access."""

    def __init__(self, clock: Clock, policy: ExitPolicy) -> None:
        self._clock = clock
        self._policy = policy

    def mark(self, position: Position, quote: OptionQuote) -> Position:
        self._require_contract(position, quote)
        pnl = self._pnl(position, quote.bid)
        return position.model_copy(
            update={
                "current_bid": quote.bid,
                "current_ask": quote.ask,
                "best_unrealized_pnl": max(position.best_unrealized_pnl, pnl),
                "worst_unrealized_pnl": min(position.worst_unrealized_pnl, pnl),
            }
        )

    def evaluate_exit(
        self,
        position: Position,
        quote: OptionQuote,
        *,
        invalidated_conditions: tuple[str, ...] = (),
        catalyst_failed: bool = False,
        hard_portfolio_emergency: bool = False,
    ) -> ExitDecision:
        self._require_contract(position, quote)
        now = self._clock.now().astimezone(UTC)
        pnl = self._pnl(position, quote.bid)
        cost = position.average_entry_price * position.quantity * position.contract.multiplier
        return_fraction = pnl / cost if cost else Decimal("0")
        triggers: list[ExitTrigger] = []

        if return_fraction >= self._policy.profit_target_fraction:
            triggers.append(ExitTrigger.PROFIT_TARGET)
        if return_fraction <= -self._policy.loss_threshold_fraction:
            triggers.append(ExitTrigger.LOSS_THRESHOLD)
        if set(invalidated_conditions).intersection(position.invalidation_conditions):
            triggers.append(ExitTrigger.THESIS_INVALIDATED)
        if catalyst_failed:
            triggers.append(ExitTrigger.CATALYST_FAILED)

        local_now = now.astimezone(ZoneInfo(self._policy.market_timezone))
        days_remaining = (position.contract.expiration - local_now.date()).days
        if days_remaining <= self._policy.sell_to_close_days_before_expiration:
            triggers.append(ExitTrigger.DTE_CUTOFF)
        if self._policy.end_of_day_exit:
            calendar = UsEquityCalendar()
            session_day = now.astimezone(calendar.timezone).date()
            session_close = calendar.regular_session_close(session_day)
            if session_close is not None:
                configured_cutoff = datetime.combine(
                    local_now.date(), self._policy.end_of_day_cutoff, tzinfo=local_now.tzinfo
                )
                # Retain an operator's earlier cutoff, but never wait past the
                # 15-minute equity-close buffer on a scheduled early-close day.
                cutoff = min(configured_cutoff, session_close - timedelta(minutes=15))
                if now >= cutoff:
                    triggers.append(ExitTrigger.END_OF_DAY)

        spread_percent = (
            ((quote.ask - quote.bid) / quote.ask) * Decimal("100")
            if quote.ask > 0
            else Decimal("Infinity")
        )
        if quote.bid <= 0 or spread_percent > self._policy.maximum_bid_ask_percent:
            triggers.append(ExitTrigger.LIQUIDITY_DETERIORATION)
        if hard_portfolio_emergency:
            triggers.append(ExitTrigger.HARD_PORTFOLIO_EMERGENCY)

        # Fresh executable quote evidence is still mandatory for an automatic
        # close. An emergency creates a decision immediately but cannot invent a
        # marketable price from stale/zero-bid data.
        quote_age = now - quote.metadata.observed_at
        quote_fresh = (
            timedelta(0) <= quote_age <= self._policy.maximum_quote_age
            and quote.metadata.effective_at <= quote.metadata.observed_at <= now
        )
        should_exit = bool(triggers)
        executable = should_exit and quote_fresh and quote.bid > 0
        if not should_exit:
            reason = "no configured deterministic exit trigger"
        elif not executable:
            reason = "exit required but fresh executable bid is unavailable"
        else:
            reason = "configured exit trigger(s): " + ",".join(
                trigger.value for trigger in triggers
            )
        return ExitDecision(
            position_id=position.position_id,
            evaluated_at=now,
            should_exit=should_exit,
            executable=executable,
            side=OrderSide.SELL_TO_CLOSE,
            quantity=position.quantity,
            limit_price=quote.bid if executable else None,
            triggers=tuple(triggers),
            unrealized_pnl=pnl,
            unrealized_return=return_fraction,
            quote_snapshot_id=quote.snapshot_id,
            policy_version=self._policy.version,
            reason=reason,
        )

    @staticmethod
    def _require_contract(position: Position, quote: OptionQuote) -> None:
        if position.contract != quote.contract:
            raise ValueError("quote does not describe the open position contract")

    @staticmethod
    def _pnl(position: Position, liquidation_price: Decimal) -> Decimal:
        return (
            (liquidation_price - position.average_entry_price)
            * position.quantity
            * position.contract.multiplier
        )
