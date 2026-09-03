from __future__ import annotations

import asyncio
import inspect
from collections.abc import Awaitable, Callable
from datetime import date, timedelta
from decimal import Decimal
from uuid import UUID

from app.broker.base import (
    BrokerCapabilities,
    CapabilityDescriptor,
    CommandIntentRecorder,
    IntentRecordingBroker,
    ReconciliationReport,
    validate_command_for_capability,
)
from app.broker.fill_models import ConservativeFillModel, FillModel
from app.broker.shadow_ledger import (
    DepositRecord,
    ExpirationPolicy,
    ExpirationResult,
    LedgerSnapshot,
    ShadowLedger,
)
from app.clock.base import Clock
from app.domain.enums import AccountKind
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
)
from app.exceptions import SafetyCriticalError

LedgerStateRecorder = Callable[[LedgerSnapshot], Awaitable[None] | None]

_PLACE_SCHEMA = {
    "type": "object",
    "properties": {
        "instrument_id": {"type": "string", "minLength": 1},
        "side": {"type": "string", "enum": ["buy_to_open", "sell_to_close"]},
        "quantity": {"type": "integer", "minimum": 1},
        "limit_price": {"type": "string"},
        "time_in_force": {"type": "string", "enum": ["day"]},
        "client_order_id": {"type": "string", "minLength": 1},
    },
    "required": ["instrument_id", "side", "quantity", "limit_price", "time_in_force"],
    "additionalProperties": False,
}

_CANCEL_SCHEMA = {
    "type": "object",
    "properties": {
        "order_id": {"type": "string", "minLength": 1},
        "client_order_id": {"type": "string", "minLength": 1},
    },
    "required": ["order_id"],
    "additionalProperties": False,
}


class SimulatedBroker(IntentRecordingBroker):
    """Credential-free broker driven exclusively by injected quote events."""

    adapter_version = "simulated-broker-v1"

    def __init__(
        self,
        *,
        clock: Clock,
        initial_cash: Decimal = Decimal("25"),
        fill_model: FillModel | None = None,
        fill_seed: int = 0,
        max_quote_age: timedelta = timedelta(seconds=30),
        namespace: str = "demo",
        command_recorder: CommandIntentRecorder | None = None,
        state_recorder: LedgerStateRecorder | None = None,
        initial_state: LedgerSnapshot | None = None,
    ) -> None:
        super().__init__(command_recorder=command_recorder)
        self._clock = clock
        self._lock = asyncio.Lock()
        self._state_recorder = state_recorder
        self._state_dirty = False
        self._ledger = ShadowLedger(
            clock=clock,
            initial_cash=initial_cash,
            account_kind=AccountKind.SIMULATED,
            fill_model=fill_model or ConservativeFillModel(seed_salt=fill_seed),
            max_quote_age=max_quote_age,
            namespace=namespace,
        )
        if initial_state is not None:
            self._ledger.restore_state(initial_state)
            self._recorded_commands = {
                command.command_intent_id: command
                for command in initial_state.recorded_commands
            }
            self._command_idempotency = {
                command.idempotency_key: command.command_intent_id
                for command in initial_state.recorded_commands
            }
            if (
                len(self._recorded_commands) != len(initial_state.recorded_commands)
                or len(self._command_idempotency) != len(initial_state.recorded_commands)
            ):
                raise SafetyCriticalError("restored broker contains duplicate command identities")
        self._capabilities = _simulated_capabilities(clock)

    @property
    def ledger(self) -> ShadowLedger:
        return self._ledger

    @property
    def state_persisted(self) -> bool:
        return not self._state_dirty

    def export_state(self) -> LedgerSnapshot:
        return self._ledger.export_state(recorded_commands=self.recorded_command_intents)

    async def flush_state(self) -> None:
        async with self._lock:
            await self._persist_state()

    def _ensure_clean(self) -> None:
        if self._state_dirty:
            raise SafetyCriticalError("simulated ledger has an unpersisted mutation")

    async def _persist_state(self) -> None:
        if self._state_recorder is None:
            return
        self._state_dirty = True
        result = self._state_recorder(self.export_state())
        if inspect.isawaitable(result):
            await result
        self._state_dirty = False

    async def get_capabilities(self) -> BrokerCapabilities:
        return self._capabilities

    async def get_observed_broker_account_state(self) -> AccountSnapshot:
        # OFFLINE_SIM has no real broker.  The observed and effective views are
        # therefore the same explicitly SIMULATED account type.
        return self._ledger.account_snapshot()

    async def get_effective_execution_account_state(self) -> AccountSnapshot:
        return self._ledger.account_snapshot()

    async def get_positions(self) -> tuple[Position, ...]:
        return self._ledger.get_positions()

    async def get_orders(self) -> tuple[BrokerOrder, ...]:
        return self._ledger.get_orders()

    async def review_option_order(self, proposal: TradeProposal) -> BrokerReview:
        async with self._lock:
            self._ensure_clean()
            review = self._ledger.review(proposal)
            await self._persist_state()
            return review

    async def place_option_order(
        self,
        command: BrokerCommandIntent,
        contract: OptionContract,
    ) -> BrokerOrder:
        validate_command_for_capability(command, self._capabilities)
        async with self._lock:
            self._ensure_clean()
            await self.record_broker_command_intent(command)
            order = self._ledger.submit(command, contract)
            await self._persist_state()
            return order

    async def cancel_option_order(
        self,
        command: BrokerCommandIntent,
        order_id: UUID | str,
    ) -> BrokerOrder:
        validate_command_for_capability(command, self._capabilities)
        async with self._lock:
            self._ensure_clean()
            await self.record_broker_command_intent(command)
            order = self._ledger.cancel(command, order_id)
            await self._persist_state()
            return order

    async def consume_quote(self, quote: OptionQuote) -> tuple[Fill, ...]:
        async with self._lock:
            self._ensure_clean()
            fills = self._ledger.observe_quote(quote)
            await self._persist_state()
            return fills

    async def deposit(
        self,
        amount: Decimal,
        *,
        reference: str = "configured-scenario",
    ) -> DepositRecord:
        async with self._lock:
            self._ensure_clean()
            deposit = self._ledger.deposit(amount, reference=reference)
            await self._persist_state()
            return deposit

    async def process_expirations(
        self,
        *,
        on_date: date | None = None,
        policy: ExpirationPolicy = ExpirationPolicy.DO_NOT_EXERCISE,
        cash_settlement_per_share: dict[str, Decimal] | None = None,
    ) -> ExpirationResult:
        async with self._lock:
            self._ensure_clean()
            result = self._ledger.expire(
                on_date=on_date,
                policy=policy,
                cash_settlement_per_share=cash_settlement_per_share,
            )
            await self._persist_state()
            return result

    async def reconcile(self) -> ReconciliationReport:
        async with self._lock:
            report = self._ledger.reconciliation_report()
            if self._state_dirty:
                return report.model_copy(
                    update={
                        "successful": False,
                        "discrepancies": (
                            *report.discrepancies,
                            "simulated ledger has an unpersisted mutation",
                        ),
                    }
                )
            return report


def _simulated_capabilities(clock: Clock) -> BrokerCapabilities:
    descriptors = (
        CapabilityDescriptor.from_schema(
            capability="get_account_state",
            tool_name="simulated.get_account_state",
            schema_version="simulated-read-v1",
            input_schema={"type": "object", "additionalProperties": False},
            side_effect_free=True,
        ),
        CapabilityDescriptor.from_schema(
            capability="get_positions",
            tool_name="simulated.get_positions",
            schema_version="simulated-read-v1",
            input_schema={"type": "object", "additionalProperties": False},
            side_effect_free=True,
        ),
        CapabilityDescriptor.from_schema(
            capability="get_orders",
            tool_name="simulated.get_orders",
            schema_version="simulated-read-v1",
            input_schema={"type": "object", "additionalProperties": False},
            side_effect_free=True,
        ),
        CapabilityDescriptor.from_schema(
            capability="review_option_order",
            tool_name="simulated.review_option_order",
            schema_version="simulated-review-v1",
            input_schema=_PLACE_SCHEMA,
            side_effect_free=True,
        ),
        CapabilityDescriptor.from_schema(
            capability="place_option_order",
            tool_name="simulated.place_option_order",
            schema_version="simulated-order-v1",
            input_schema=_PLACE_SCHEMA,
            side_effect_free=False,
        ),
        CapabilityDescriptor.from_schema(
            capability="cancel_option_order",
            tool_name="simulated.cancel_option_order",
            schema_version="simulated-order-v1",
            input_schema=_CANCEL_SCHEMA,
            side_effect_free=False,
        ),
    )
    return BrokerCapabilities(
        adapter_name="SimulatedBroker",
        adapter_version=SimulatedBroker.adapter_version,
        discovered_at=clock.now(),
        descriptors=descriptors,
        account_state=True,
        positions=True,
        orders=True,
        review_option_orders=True,
        place_option_orders=True,
        cancel_option_orders=True,
        reconcile=True,
        external_writes_enabled=False,
        execution_ready=True,
    )
