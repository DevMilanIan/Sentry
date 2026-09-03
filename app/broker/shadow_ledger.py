from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from enum import StrEnum
from types import MappingProxyType
from typing import Literal
from uuid import NAMESPACE_URL, UUID, uuid5
from zoneinfo import ZoneInfo

from pydantic import Field, field_validator

from app.broker.base import BrokerValueModel, ReconciliationReport
from app.broker.fill_models import ConservativeFillModel, FillModel
from app.clock.base import Clock
from app.domain.enums import (
    AccountKind,
    BrokerAction,
    ExecutionEnvironment,
    OrderSide,
    OrderState,
)
from app.domain.models import (
    AccountSnapshot,
    BrokerCommandIntent,
    BrokerOrder,
    BrokerReview,
    Fill,
    OptionContract,
    OptionQuote,
    Position,
    TradeProposal,
    sha256_json,
)
from app.exceptions import DataInvalidError, SafetyCriticalError

_OPEN_STATES = frozenset({OrderState.OPEN, OrderState.PARTIAL})
_TERMINAL_STATES = frozenset(
    {OrderState.FILLED, OrderState.CANCELED, OrderState.REJECTED, OrderState.EXPIRED}
)
_MARKET_TZ = ZoneInfo("America/New_York")


class ExpirationPolicy(StrEnum):
    """Local long-option expiration outcomes; neither can create shares."""

    DO_NOT_EXERCISE = "DO_NOT_EXERCISE"
    CASH_SETTLE = "CASH_SETTLE"


class DepositRecord(BrokerValueModel):
    amount: Decimal = Field(gt=0)
    deposited_at: datetime
    reference: str

    @field_validator("deposited_at")
    @classmethod
    def timestamp_is_utc(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("deposited_at must be timezone-aware")
        return value.astimezone(UTC)


class ExpirationResult(BrokerValueModel):
    processed_at: datetime
    expiration_date: date
    expired_order_ids: tuple[UUID, ...] = ()
    expired_instrument_ids: tuple[str, ...] = ()
    cash_settlement: Decimal = Field(default=Decimal("0"), ge=0)
    realized_pnl: Decimal = Decimal("0")
    policy: ExpirationPolicy
    reason: str

    @field_validator("processed_at")
    @classmethod
    def processed_at_is_utc(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("processed_at must be timezone-aware")
        return value.astimezone(UTC)


@dataclass(slots=True)
class _LedgerOrder:
    published: BrokerOrder
    command: BrokerCommandIntent


class LedgerOrderSnapshot(BrokerValueModel):
    published: BrokerOrder
    command: BrokerCommandIntent


class LedgerSnapshot(BrokerValueModel):
    version: Literal["shadow-ledger-state-v1"] = "shadow-ledger-state-v1"
    recorded_at: datetime
    environment: ExecutionEnvironment
    account_kind: AccountKind
    namespace: str
    initial_cash: Decimal = Field(ge=0)
    cash: Decimal = Field(ge=0)
    fill_model_version: str
    orders: tuple[LedgerOrderSnapshot, ...]
    idempotency_order_ids: dict[str, UUID]
    positions: tuple[Position, ...]
    fills: tuple[Fill, ...]
    quotes: tuple[OptionQuote, ...]
    proposals: tuple[TradeProposal, ...]
    rejection_reasons: dict[UUID, tuple[str, ...]]
    deposits: tuple[DepositRecord, ...]
    entry_orders_by_date: dict[date, tuple[UUID, ...]]
    realized_pnl: Decimal
    closed_realized_pnl: dict[str, Decimal]
    recorded_commands: tuple[BrokerCommandIntent, ...] = ()

    @field_validator("recorded_at")
    @classmethod
    def recorded_at_is_utc(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("ledger snapshot timestamp must be timezone-aware")
        return value.astimezone(UTC)

    @property
    def content_hash(self) -> str:
        return sha256_json(self.model_dump(mode="json"))


class ShadowLedger:
    """Deterministic long-option cash ledger shared by Demo broker backends.

    It owns no clock and no market-data connection.  Time and normalized quote
    events are injected, which makes replay causally checkable and reproducible.
    """

    def __init__(
        self,
        *,
        clock: Clock,
        initial_cash: Decimal = Decimal("25"),
        account_kind: AccountKind = AccountKind.SHADOW,
        environment: ExecutionEnvironment = ExecutionEnvironment.DEMO,
        fill_model: FillModel | None = None,
        max_quote_age: timedelta = timedelta(seconds=30),
        namespace: str = "demo",
    ) -> None:
        if environment is not ExecutionEnvironment.DEMO:
            raise ValueError("ShadowLedger can only model the DEMO environment")
        if account_kind not in {AccountKind.SHADOW, AccountKind.SIMULATED}:
            raise ValueError("ledger account kind must be SHADOW or SIMULATED")
        if initial_cash < 0:
            raise ValueError("initial_cash cannot be negative")
        if max_quote_age.total_seconds() <= 0:
            raise ValueError("max_quote_age must be positive")
        if not namespace:
            raise ValueError("namespace cannot be empty")

        self._clock = clock
        self._initial_cash = Decimal(initial_cash)
        self._cash = Decimal(initial_cash)
        self._account_kind = account_kind
        self._environment = environment
        self._fill_model = fill_model or ConservativeFillModel()
        self._max_quote_age = max_quote_age
        self._namespace = namespace
        self._orders: dict[UUID, _LedgerOrder] = {}
        self._order_by_idempotency: dict[str, UUID] = {}
        self._positions: dict[str, Position] = {}
        self._fills: list[Fill] = []
        self._quotes: dict[str, OptionQuote] = {}
        self._proposals: dict[UUID, TradeProposal] = {}
        self._rejection_reasons: dict[UUID, tuple[str, ...]] = {}
        self._deposits: list[DepositRecord] = []
        self._entry_orders_by_date: dict[date, set[UUID]] = {}
        self._realized_pnl = Decimal("0")
        self._closed_realized_pnl: dict[str, Decimal] = {}

    @property
    def fill_model(self) -> FillModel:
        return self._fill_model

    @property
    def cash(self) -> Decimal:
        return self._cash

    @property
    def buying_power(self) -> Decimal:
        return self._cash - self._reserved_buying_power()

    @property
    def realized_pnl(self) -> Decimal:
        return self._realized_pnl

    @property
    def fills(self) -> tuple[Fill, ...]:
        return tuple(self._fills)

    @property
    def deposits(self) -> tuple[DepositRecord, ...]:
        return tuple(self._deposits)

    @property
    def rejection_reasons(self) -> MappingProxyType[UUID, tuple[str, ...]]:
        return MappingProxyType(self._rejection_reasons)

    def get_orders(self) -> tuple[BrokerOrder, ...]:
        return tuple(item.published for item in self._orders.values())

    def get_positions(self) -> tuple[Position, ...]:
        return tuple(self._positions.values())

    def latest_quote(self, instrument_id: str) -> OptionQuote | None:
        return self._quotes.get(instrument_id)

    def export_state(
        self, *, recorded_commands: tuple[BrokerCommandIntent, ...] = ()
    ) -> LedgerSnapshot:
        return LedgerSnapshot(
            recorded_at=self._clock.now(),
            environment=self._environment,
            account_kind=self._account_kind,
            namespace=self._namespace,
            initial_cash=self._initial_cash,
            cash=self._cash,
            fill_model_version=self._fill_model.version,
            orders=tuple(
                LedgerOrderSnapshot(published=item.published, command=item.command)
                for item in self._orders.values()
            ),
            idempotency_order_ids=dict(self._order_by_idempotency),
            positions=self.get_positions(),
            fills=self.fills,
            quotes=tuple(self._quotes.values()),
            proposals=tuple(self._proposals.values()),
            rejection_reasons=dict(self._rejection_reasons),
            deposits=self.deposits,
            entry_orders_by_date={
                day: tuple(sorted(order_ids, key=str))
                for day, order_ids in self._entry_orders_by_date.items()
            },
            realized_pnl=self._realized_pnl,
            closed_realized_pnl=dict(self._closed_realized_pnl),
            recorded_commands=recorded_commands,
        )

    def restore_state(self, snapshot: LedgerSnapshot) -> None:
        """Restore only the same isolated ledger identity and fill-model version."""
        if (
            snapshot.environment is not self._environment
            or snapshot.account_kind is not self._account_kind
            or snapshot.namespace != self._namespace
        ):
            raise SafetyCriticalError("ledger snapshot identity does not match startup binding")
        if snapshot.initial_cash != self._initial_cash:
            raise SafetyCriticalError("initial cash changed while restoring an existing ledger")
        if snapshot.fill_model_version != self._fill_model.version:
            raise SafetyCriticalError("fill model changed while restoring an existing ledger")
        if snapshot.recorded_at > self._clock.now():
            raise DataInvalidError("ledger snapshot is newer than the injected clock")
        order_ids = {item.published.order_id for item in snapshot.orders}
        if len(order_ids) != len(snapshot.orders):
            raise SafetyCriticalError("ledger snapshot contains duplicate order IDs")
        if set(snapshot.idempotency_order_ids.values()) - order_ids:
            raise SafetyCriticalError("ledger idempotency map references an absent order")
        for item in snapshot.orders:
            if (
                item.published.environment is not self._environment
                or item.command.environment is not self._environment
                or item.command.namespace != self._namespace
                or item.published.intent_id != item.command.order_intent_id
            ):
                raise SafetyCriticalError("ledger order snapshot is cross-boundary or uncorrelated")
        for command in snapshot.recorded_commands:
            if command.environment is not self._environment or command.namespace != self._namespace:
                raise SafetyCriticalError("recorded command does not match ledger identity")
        for position in snapshot.positions:
            if position.environment is not self._environment:
                raise SafetyCriticalError("ledger position snapshot crosses environments")
        if len({item.contract.instrument_id for item in snapshot.positions}) != len(
            snapshot.positions
        ):
            raise SafetyCriticalError("ledger snapshot contains duplicate positions")
        if any(fill.order_id not in order_ids for fill in snapshot.fills):
            raise SafetyCriticalError("ledger fill references an absent order")
        if any(
            quote.metadata.observed_at > snapshot.recorded_at
            or quote.metadata.effective_at > snapshot.recorded_at
            for quote in snapshot.quotes
        ):
            raise DataInvalidError("ledger snapshot contains a future quote")

        restored = ShadowLedger(
            clock=self._clock,
            initial_cash=self._initial_cash,
            account_kind=self._account_kind,
            environment=self._environment,
            fill_model=self._fill_model,
            max_quote_age=self._max_quote_age,
            namespace=self._namespace,
        )
        restored._apply_snapshot(snapshot)
        issues = restored._invariant_discrepancies()
        if issues:
            raise SafetyCriticalError("invalid restored ledger: " + "; ".join(issues))
        self._apply_snapshot(snapshot)

    def _apply_snapshot(self, snapshot: LedgerSnapshot) -> None:
        self._cash = snapshot.cash
        self._orders = {
            item.published.order_id: _LedgerOrder(item.published, item.command)
            for item in snapshot.orders
        }
        self._order_by_idempotency = dict(snapshot.idempotency_order_ids)
        self._positions = {item.contract.instrument_id: item for item in snapshot.positions}
        self._fills = list(snapshot.fills)
        self._quotes = {item.contract.instrument_id: item for item in snapshot.quotes}
        self._proposals = {item.proposal_id: item for item in snapshot.proposals}
        self._rejection_reasons = dict(snapshot.rejection_reasons)
        self._deposits = list(snapshot.deposits)
        self._entry_orders_by_date = {
            day: set(order_ids) for day, order_ids in snapshot.entry_orders_by_date.items()
        }
        self._realized_pnl = snapshot.realized_pnl
        self._closed_realized_pnl = dict(snapshot.closed_realized_pnl)

    def account_snapshot(self) -> AccountSnapshot:
        now = self._clock.now()
        market_date = now.astimezone(_MARKET_TZ).date()
        pending_instruments = {
            item.published.contract.instrument_id
            for item in self._orders.values()
            if item.published.state in _OPEN_STATES and item.published.side is OrderSide.BUY_TO_OPEN
        }
        return AccountSnapshot(
            created_at=now,
            environment=self._environment,
            account_kind=self._account_kind,
            account_fingerprint=None,
            cash=self._cash,
            buying_power=max(Decimal("0"), self.buying_power),
            open_option_risk=sum(
                (
                    position.average_entry_price * position.quantity * position.contract.multiplier
                    for position in self._positions.values()
                ),
                Decimal("0"),
            )
            + self._reserved_buying_power(),
            open_positions=len(set(self._positions) | pending_instruments),
            new_entries_today=len(self._entry_orders_by_date.get(market_date, set())),
            as_of=now,
            is_authenticated=True,
            state_known=True,
        )

    def deposit(self, amount: Decimal, *, reference: str = "configured-scenario") -> DepositRecord:
        amount = Decimal(amount)
        if amount <= 0:
            raise ValueError("deposit amount must be positive")
        record = DepositRecord(amount=amount, deposited_at=self._clock.now(), reference=reference)
        self._cash += amount
        self._deposits.append(record)
        return record

    def observe_quote(self, quote: OptionQuote) -> tuple[Fill, ...]:
        """Apply one externally supplied quote, rejecting future/out-of-order data."""

        now = self._clock.now()
        effective_at = quote.metadata.effective_at
        if effective_at > now or quote.metadata.observed_at > now:
            raise DataInvalidError("future option quote would violate replay causality")

        instrument_id = quote.contract.instrument_id
        prior = self._quotes.get(instrument_id)
        if prior is not None and prior.snapshot_id == quote.snapshot_id:
            if prior != quote:
                raise DataInvalidError("quote snapshot ID was reused with different content")
            return ()
        if prior is not None and effective_at < prior.metadata.effective_at:
            raise DataInvalidError("out-of-order option quote cannot rewind ledger state")
        if prior is not None and prior.contract != quote.contract:
            raise DataInvalidError("option instrument ID was reused for a different contract")
        self._quotes[instrument_id] = quote
        self._mark_position(quote)
        if now - quote.metadata.observed_at > self._max_quote_age:
            # Stale events may mark the last-known display value but can never
            # create an order fill.
            return ()

        produced: list[Fill] = []
        for ledger_order in tuple(self._orders.values()):
            order = ledger_order.published
            if order.contract.instrument_id != instrument_id or order.state not in _OPEN_STATES:
                continue
            # A decision/order cannot be filled by the same or an earlier event.
            if order.submitted_at is None or effective_at <= order.submitted_at:
                continue
            remaining = order.quantity - order.filled_quantity
            decision = self._fill_model.evaluate(
                order=order,
                quote=quote,
                remaining_quantity=remaining,
            )
            if not decision.should_fill:
                continue
            assert decision.price is not None
            fill = self._apply_fill(
                ledger_order,
                quote=quote,
                quantity=decision.quantity,
                price=decision.price,
                deterministic_seed=decision.deterministic_seed,
                reason=decision.reason,
            )
            produced.append(fill)
        return tuple(produced)

    def review(self, proposal: TradeProposal) -> BrokerReview:
        now = self._clock.now()
        warnings: list[str] = []
        if proposal.environment is not self._environment:
            warnings.append("proposal execution environment does not match ledger")
        if proposal.namespace != self._namespace:
            warnings.append("proposal namespace does not match ledger")
        if proposal.contract.expiration < now.astimezone(_MARKET_TZ).date():
            warnings.append("option contract is expired")

        quote = self._quotes.get(proposal.contract.instrument_id)
        if quote is None:
            warnings.append("no externally supplied quote is available")
        else:
            if quote.snapshot_id != proposal.quote_snapshot_id:
                warnings.append("proposal does not reference the latest quote snapshot")
            if quote.metadata.effective_at > now or quote.metadata.observed_at > now:
                warnings.append("quote is from the future")
            elif now - quote.metadata.observed_at > self._max_quote_age:
                warnings.append("option quote is stale")

        if proposal.side is OrderSide.BUY_TO_OPEN:
            if proposal.max_loss > self.buying_power:
                warnings.append("insufficient simulated buying power")
        else:
            available = self._available_position_quantity(proposal.contract.instrument_id)
            if proposal.quantity > available:
                warnings.append("insufficient unreserved long position to close")

        self._proposals[proposal.proposal_id] = proposal
        return BrokerReview(
            created_at=now,
            environment=self._environment,
            proposal_id=proposal.proposal_id,
            accepted=not warnings,
            warnings=tuple(warnings),
            raw_reference=f"{self._account_kind.value.lower()}-ledger-review",
            side_effect_free=True,
        )

    def submit(self, command: BrokerCommandIntent, contract: OptionContract) -> BrokerOrder:
        self._validate_command(command, contract, BrokerAction.PLACE_OPTION_ORDER)
        duplicate = self._duplicate_order(command)
        if duplicate is not None:
            return duplicate

        now = self._clock.now()
        reasons: list[str] = []
        if contract.expiration < now.astimezone(_MARKET_TZ).date():
            reasons.append("option contract is expired")
        if command.side is OrderSide.BUY_TO_OPEN:
            required = command.limit_price * command.quantity * contract.multiplier
            if required > self.buying_power:
                reasons.append("insufficient simulated buying power")
        elif command.quantity > self._available_position_quantity(contract.instrument_id):
            reasons.append("insufficient unreserved long position to close")

        order_id = uuid5(NAMESPACE_URL, f"options-sentinel:{command.order_intent_id}")
        order = BrokerOrder(
            order_id=order_id,
            broker_order_id=None,
            intent_id=command.order_intent_id,
            environment=self._environment,
            state=OrderState.REJECTED if reasons else OrderState.OPEN,
            contract=contract,
            side=command.side,
            quantity=command.quantity,
            filled_quantity=0,
            limit_price=command.limit_price,
            average_fill_price=None,
            submitted_at=now,
            created_at=now,
        )
        self._orders[order_id] = _LedgerOrder(published=order, command=command)
        self._order_by_idempotency[command.idempotency_key] = order_id
        if reasons:
            self._rejection_reasons[order_id] = tuple(reasons)
        elif command.side is OrderSide.BUY_TO_OPEN:
            # Reserve the daily admission budget at acceptance, not at fill.
            # Cancellation does not refund admission and permit infinite churn.
            self._entry_orders_by_date.setdefault(now.astimezone(_MARKET_TZ).date(), set()).add(
                order.order_id
            )
        return order

    def cancel(self, command: BrokerCommandIntent, order_id: UUID | str) -> BrokerOrder:
        if command.action is not BrokerAction.CANCEL_OPTION_ORDER:
            raise DataInvalidError("cancel operation requires CANCEL_OPTION_ORDER intent")
        if command.environment is not self._environment:
            raise SafetyCriticalError("command environment does not match ledger")
        duplicate = self._duplicate_order(command)
        if duplicate is not None:
            return duplicate

        ledger_order = self._find_order(order_id)
        order = ledger_order.published
        if command.instrument_id != order.contract.instrument_id:
            raise DataInvalidError("cancel command instrument does not match target order")
        if order.state in _TERMINAL_STATES:
            self._order_by_idempotency[command.idempotency_key] = order.order_id
            return order
        if order.state not in _OPEN_STATES:
            raise SafetyCriticalError(f"cannot cancel order in state {order.state.value}")
        canceled = order.model_copy(update={"state": OrderState.CANCELED})
        ledger_order.published = canceled
        self._order_by_idempotency[command.idempotency_key] = canceled.order_id
        return canceled

    def expire(
        self,
        *,
        on_date: date | None = None,
        policy: ExpirationPolicy = ExpirationPolicy.DO_NOT_EXERCISE,
        cash_settlement_per_share: dict[str, Decimal] | None = None,
    ) -> ExpirationResult:
        """Expire due orders/positions without ever creating underlying shares."""

        now = self._clock.now()
        expiration_date = on_date or now.astimezone(_MARKET_TZ).date()
        if expiration_date > now.astimezone(_MARKET_TZ).date():
            raise DataInvalidError("cannot process a future expiration date")
        settlements = cash_settlement_per_share or {}
        expired_order_ids: list[UUID] = []
        expired_instruments: list[str] = []
        total_settlement = Decimal("0")
        total_realized = Decimal("0")

        for ledger_order in self._orders.values():
            order = ledger_order.published
            submitted_date = (
                order.submitted_at.astimezone(_MARKET_TZ).date()
                if order.submitted_at is not None
                else expiration_date
            )
            day_order_ended = (
                ledger_order.command.time_in_force.lower() == "day"
                and submitted_date <= expiration_date
            )
            contract_expired = order.contract.expiration <= expiration_date
            if order.state in _OPEN_STATES and (day_order_ended or contract_expired):
                ledger_order.published = order.model_copy(update={"state": OrderState.EXPIRED})
                expired_order_ids.append(order.order_id)

        for instrument_id, position in tuple(self._positions.items()):
            if position.contract.expiration > expiration_date:
                continue
            settlement_per_share = Decimal(settlements.get(instrument_id, Decimal("0")))
            if settlement_per_share < 0:
                raise ValueError("cash settlement cannot be negative")
            if policy is ExpirationPolicy.DO_NOT_EXERCISE:
                settlement_per_share = Decimal("0")
            proceeds = settlement_per_share * position.quantity * position.contract.multiplier
            pnl = (
                (settlement_per_share - position.average_entry_price)
                * position.quantity
                * position.contract.multiplier
            )
            self._cash += proceeds
            self._realized_pnl += pnl
            self._closed_realized_pnl[instrument_id] = (
                self._closed_realized_pnl.get(instrument_id, Decimal("0"))
                + position.realized_pnl
                + pnl
            )
            total_settlement += proceeds
            total_realized += pnl
            expired_instruments.append(instrument_id)
            del self._positions[instrument_id]

        reason = (
            "expired long options were cash-settled; no shares were created"
            if policy is ExpirationPolicy.CASH_SETTLE
            else "expired long options were not exercised; no shares were created"
        )
        return ExpirationResult(
            processed_at=now,
            expiration_date=expiration_date,
            expired_order_ids=tuple(expired_order_ids),
            expired_instrument_ids=tuple(expired_instruments),
            cash_settlement=total_settlement,
            realized_pnl=total_realized,
            policy=policy,
            reason=reason,
        )

    def reconciliation_report(self) -> ReconciliationReport:
        discrepancies = self._invariant_discrepancies()
        account = self.account_snapshot()
        return ReconciliationReport(
            environment=self._environment,
            reconciled_at=self._clock.now(),
            successful=not discrepancies,
            observed_account=account,
            effective_account=account,
            position_count=len(self._positions),
            open_order_count=sum(
                item.published.state in _OPEN_STATES for item in self._orders.values()
            ),
            discrepancies=tuple(discrepancies),
            details={
                "account_kind": self._account_kind.value,
                "fill_model_version": self._fill_model.version,
                "fill_count": len(self._fills),
                "realized_pnl": str(self._realized_pnl),
            },
        )

    def _duplicate_order(self, command: BrokerCommandIntent) -> BrokerOrder | None:
        existing_id = self._order_by_idempotency.get(command.idempotency_key)
        if existing_id is None:
            return None
        ledger_order = self._orders[existing_id]
        # A cancel intent may map to its target order, so compare command hashes
        # only for repeated placement intents.
        if command.action is BrokerAction.PLACE_OPTION_ORDER:
            if ledger_order.command.command_hash != command.command_hash:
                raise SafetyCriticalError(
                    "ledger idempotency key was reused with different content"
                )
        return ledger_order.published

    def _validate_command(
        self,
        command: BrokerCommandIntent,
        contract: OptionContract,
        expected_action: BrokerAction,
    ) -> None:
        if command.environment is not self._environment:
            raise SafetyCriticalError("command environment does not match ledger")
        if command.namespace != self._namespace:
            raise SafetyCriticalError("command namespace does not match ledger")
        if command.action is not expected_action:
            raise DataInvalidError(f"operation requires {expected_action.value} intent")
        if command.instrument_id != contract.instrument_id:
            raise DataInvalidError("command instrument does not match option contract")
        proposal = self._proposals.get(command.proposal_id)
        if proposal is not None:
            if proposal.contract.instrument_id != command.instrument_id:
                raise SafetyCriticalError("reviewed proposal and command instrument differ")
            if proposal.side is not command.side or proposal.quantity != command.quantity:
                raise SafetyCriticalError("reviewed proposal and command order terms differ")
            if proposal.limit_price != command.limit_price:
                raise SafetyCriticalError("reviewed proposal and command limit differ")

    def _find_order(self, order_id: UUID | str) -> _LedgerOrder:
        if isinstance(order_id, UUID):
            found = self._orders.get(order_id)
            if found is not None:
                return found
        else:
            try:
                local_id = UUID(order_id)
            except ValueError:
                local_id = None
            if local_id is not None and local_id in self._orders:
                return self._orders[local_id]
            for item in self._orders.values():
                if item.published.broker_order_id == order_id:
                    return item
        raise DataInvalidError(f"unknown ledger order: {order_id}")

    def _apply_fill(
        self,
        ledger_order: _LedgerOrder,
        *,
        quote: OptionQuote,
        quantity: int,
        price: Decimal,
        deterministic_seed: int,
        reason: str,
    ) -> Fill:
        order = ledger_order.published
        remaining = order.quantity - order.filled_quantity
        if quantity <= 0 or quantity > remaining:
            raise SafetyCriticalError("fill model returned an invalid quantity")
        if price <= 0:
            raise SafetyCriticalError("fill model returned a non-positive price")
        if order.side is OrderSide.BUY_TO_OPEN and price > order.limit_price:
            raise SafetyCriticalError("buy fill exceeded its limit")
        if order.side is OrderSide.SELL_TO_CLOSE and price < order.limit_price:
            raise SafetyCriticalError("sell fill was below its limit")

        notional = price * quantity * order.contract.multiplier
        if order.side is OrderSide.BUY_TO_OPEN:
            if notional > self._cash:
                raise SafetyCriticalError("simulated fill would make cash negative")
            self._cash -= notional
            self._apply_entry_position(ledger_order.command, order.contract, quote, quantity, price)
            market_date = self._clock.now().astimezone(_MARKET_TZ).date()
            self._entry_orders_by_date.setdefault(market_date, set()).add(order.order_id)
        else:
            self._apply_exit_position(order.contract, quantity, price)
            self._cash += notional

        prior_quantity = order.filled_quantity
        filled_quantity = prior_quantity + quantity
        prior_value = (order.average_fill_price or Decimal("0")) * prior_quantity
        average = (prior_value + price * quantity) / filled_quantity
        state = OrderState.FILLED if filled_quantity == order.quantity else OrderState.PARTIAL
        ledger_order.published = order.model_copy(
            update={
                "state": state,
                "filled_quantity": filled_quantity,
                "average_fill_price": average,
            }
        )
        fill = Fill(
            created_at=self._clock.now(),
            order_id=order.order_id,
            quantity=quantity,
            price=price,
            market_event_ids=(str(quote.snapshot_id),),
            fill_model_version=self._fill_model.version,
            deterministic_seed=deterministic_seed,
            reason=reason,
        )
        self._fills.append(fill)
        return fill

    def _apply_entry_position(
        self,
        command: BrokerCommandIntent,
        contract: OptionContract,
        quote: OptionQuote,
        quantity: int,
        price: Decimal,
    ) -> None:
        existing = self._positions.get(contract.instrument_id)
        proposal = self._proposals.get(command.proposal_id)
        if existing is None:
            unrealized = (quote.bid - price) * quantity * contract.multiplier
            self._positions[contract.instrument_id] = Position(
                created_at=self._clock.now(),
                environment=self._environment,
                contract=contract,
                quantity=quantity,
                average_entry_price=price,
                current_bid=quote.bid,
                current_ask=quote.ask,
                realized_pnl=Decimal("0"),
                best_unrealized_pnl=unrealized,
                worst_unrealized_pnl=unrealized,
                thesis_id=proposal.packet_id if proposal is not None else command.proposal_id,
                invalidation_conditions=(
                    proposal.invalidation_conditions if proposal is not None else ()
                ),
                exit_policy_version=(
                    proposal.policy_version if proposal is not None else "unknown"
                ),
            )
            return

        total_quantity = existing.quantity + quantity
        average = (
            existing.average_entry_price * existing.quantity + price * quantity
        ) / total_quantity
        self._positions[contract.instrument_id] = existing.model_copy(
            update={
                "quantity": total_quantity,
                "average_entry_price": average,
                "current_bid": quote.bid,
                "current_ask": quote.ask,
            }
        )
        self._mark_position(quote)

    def _apply_exit_position(
        self,
        contract: OptionContract,
        quantity: int,
        price: Decimal,
    ) -> None:
        position = self._positions.get(contract.instrument_id)
        if position is None or quantity > position.quantity:
            raise SafetyCriticalError("sell fill exceeds the simulated long position")
        pnl = (price - position.average_entry_price) * quantity * contract.multiplier
        self._realized_pnl += pnl
        remaining = position.quantity - quantity
        if remaining == 0:
            self._closed_realized_pnl[contract.instrument_id] = (
                self._closed_realized_pnl.get(contract.instrument_id, Decimal("0"))
                + position.realized_pnl
                + pnl
            )
            del self._positions[contract.instrument_id]
            return
        self._positions[contract.instrument_id] = position.model_copy(
            update={
                "quantity": remaining,
                "realized_pnl": position.realized_pnl + pnl,
            }
        )

    def _mark_position(self, quote: OptionQuote) -> None:
        position = self._positions.get(quote.contract.instrument_id)
        if position is None:
            return
        unrealized = (
            (quote.bid - position.average_entry_price)
            * position.quantity
            * position.contract.multiplier
        )
        self._positions[quote.contract.instrument_id] = position.model_copy(
            update={
                "current_bid": quote.bid,
                "current_ask": quote.ask,
                "best_unrealized_pnl": max(position.best_unrealized_pnl, unrealized),
                "worst_unrealized_pnl": min(position.worst_unrealized_pnl, unrealized),
            }
        )

    def _reserved_buying_power(self) -> Decimal:
        return sum(
            (
                (item.published.quantity - item.published.filled_quantity)
                * item.published.limit_price
                * item.published.contract.multiplier
                for item in self._orders.values()
                if item.published.state in _OPEN_STATES
                and item.published.side is OrderSide.BUY_TO_OPEN
            ),
            Decimal("0"),
        )

    def _available_position_quantity(self, instrument_id: str) -> int:
        position = self._positions.get(instrument_id)
        owned = position.quantity if position is not None else 0
        reserved = sum(
            item.published.quantity - item.published.filled_quantity
            for item in self._orders.values()
            if item.published.state in _OPEN_STATES
            and item.published.side is OrderSide.SELL_TO_CLOSE
            and item.published.contract.instrument_id == instrument_id
        )
        return owned - reserved

    def _invariant_discrepancies(self) -> list[str]:
        issues: list[str] = []
        if self._cash < 0:
            issues.append("ledger cash is negative")
        if self.buying_power < 0:
            issues.append("open buy reservations exceed cash")
        for item in self._orders.values():
            order = item.published
            if not 0 <= order.filled_quantity <= order.quantity:
                issues.append(f"order {order.order_id} has invalid filled quantity")
            if order.state is OrderState.FILLED and order.filled_quantity != order.quantity:
                issues.append(f"filled order {order.order_id} is incomplete")
        for instrument_id, position in self._positions.items():
            if position.quantity <= 0:
                issues.append(f"position {instrument_id} is not positive")
            if self._available_position_quantity(instrument_id) < 0:
                issues.append(f"position {instrument_id} is oversubscribed by close orders")
        return issues
