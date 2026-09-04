from __future__ import annotations

import asyncio
from datetime import timedelta
from decimal import Decimal
from uuid import UUID, uuid4

import pytest

from app.broker.base import BrokerCapabilities, ReconciliationReport
from app.broker.simulated import SimulatedBroker
from app.clock.base import VirtualClock
from app.config import load_config
from app.domain.enums import (
    AccountKind,
    ExecutionEnvironment,
    OrderState,
    RuntimeSafetyState,
    TradingMode,
)
from app.domain.models import (
    AccountSnapshot,
    BrokerCommandIntent,
    BrokerOrder,
    BrokerReview,
    OptionContract,
    OptionQuote,
    ProviderMetadata,
    TradeProposal,
)
from app.execution.service import (
    ExecutionDenied,
    ExecutionService,
    InMemoryExecutionStore,
    StateTransitionRecord,
)
from app.risk import RiskEngine
from app.safety.runtime_state import SafetyController, SafetyEvidence


class _Quote:
    def __init__(self, value: OptionQuote) -> None:
        self.value = value

    async def get_option_quote(self, instrument_id: str) -> OptionQuote:
        assert instrument_id == self.value.contract.instrument_id
        return self.value


class _MemoryBroker(SimulatedBroker):
    """No transport exists; LIVE is a typed mock label, never account access."""

    def __init__(
        self, clock: VirtualClock, environment: ExecutionEnvironment,
        store: InMemoryExecutionStore, block_action: str,
    ) -> None:
        super().__init__(clock=clock, namespace="cancellation-test")
        self.environment = environment
        self.store = store
        self.block_action = block_action
        self.entered = asyncio.Event()
        self.release = asyncio.Event()
        self.calls: list[str] = []
        self.memory_orders: dict[str, BrokerOrder] = {}

    async def get_capabilities(self) -> BrokerCapabilities:
        capabilities = await super().get_capabilities()
        return capabilities.model_copy(update={
            "adapter_name": "memory-only-cancellation-test",
            "external_writes_enabled": self.environment is ExecutionEnvironment.LIVE,
        })

    async def get_observed_broker_account_state(self) -> AccountSnapshot:
        return AccountSnapshot(
            created_at=self._clock.now(), environment=self.environment,
            account_kind=AccountKind.BROKER_OBSERVED
            if self.environment is ExecutionEnvironment.LIVE else AccountKind.SHADOW,
            account_fingerprint="mock-only", cash=Decimal("25"), buying_power=Decimal("25"),
            as_of=self._clock.now(), is_authenticated=True, state_known=True,
        )

    async def get_effective_execution_account_state(self) -> AccountSnapshot:
        return await self.get_observed_broker_account_state()

    async def get_orders(self) -> tuple[BrokerOrder, ...]:
        return tuple(self.memory_orders.values())

    async def reconcile(self) -> ReconciliationReport:
        account = await self.get_observed_broker_account_state()
        return ReconciliationReport(
            environment=self.environment, reconciled_at=self._clock.now(), successful=True,
            observed_account=account, effective_account=account,
            position_count=0, open_order_count=len(self.memory_orders),
        )

    async def review_option_order(self, proposal: TradeProposal) -> BrokerReview:
        if self.block_action == "review":
            self.entered.set()
            await self.release.wait()
        return BrokerReview(
            created_at=self._clock.now(), environment=self.environment,
            proposal_id=proposal.proposal_id, accepted=True, side_effect_free=True,
        )

    async def _boundary(self, action: str, command: BrokerCommandIntent) -> None:
        assert command.command_intent_id in self.store.command_intents
        assert self.store.orders[command.order_intent_id].state is OrderState.SUBMITTING
        self.calls.append(action)
        if self.block_action == action:
            self.entered.set()
            await self.release.wait()

    async def place_option_order(
        self, command: BrokerCommandIntent, contract: OptionContract
    ) -> BrokerOrder:
        await self._boundary("place", command)
        order = BrokerOrder(
            order_id=uuid4(), created_at=self._clock.now(), intent_id=command.order_intent_id,
            environment=self.environment, state=OrderState.OPEN, contract=contract,
            side=command.side, quantity=command.quantity, limit_price=command.limit_price,
            submitted_at=self._clock.now(), broker_order_id="memory-order",
        )
        self.memory_orders["memory-order"] = order
        return order

    async def cancel_option_order(
        self, command: BrokerCommandIntent, order_id: UUID | str
    ) -> BrokerOrder:
        await self._boundary("cancel", command)
        return self.memory_orders[str(order_id)].model_copy(update={"state": OrderState.CANCELED})


class _FaultStore(InMemoryExecutionStore):
    def __init__(self, failure: str = "") -> None:
        super().__init__()
        self.failure = failure
        self.audit_entered = asyncio.Event()

    async def save_order(self, order: BrokerOrder) -> None:
        if order.state is OrderState.SUBMISSION_UNKNOWN:
            self.audit_entered.set()
            if self.failure == "save":
                raise OSError("sensitive persistence failure")
            if self.failure in {"timeout", "second_cancel"}:
                await asyncio.Event().wait()
        await super().save_order(order)

    async def record_transition(self, record: StateTransitionRecord) -> None:
        if record.current is OrderState.SUBMISSION_UNKNOWN and self.failure == "transition":
            raise OSError("sensitive transition failure")
        await super().record_transition(record)


def _service(
    clock: VirtualClock, trade: TradeProposal, environment: ExecutionEnvironment,
    action: str, failure: str = "",
) -> tuple[ExecutionService, _MemoryBroker, _FaultStore, SafetyController, TradeProposal]:
    quote = OptionQuote(
        contract=trade.contract, bid=Decimal("0.07"), ask=Decimal("0.08"),
        volume=100, open_interest=500, bid_size=10, ask_size=10,
        metadata=ProviderMetadata(
            provider="fixture", capability_version="v1",
            observed_at=clock.now(), effective_at=clock.now(),
        ),
    )
    trade = trade.model_copy(update={
        "environment": environment, "namespace": "cancellation-test",
        "quote_snapshot_id": quote.snapshot_id,
    })
    store = _FaultStore(failure)
    broker = _MemoryBroker(clock, environment, store, action)
    safety = SafetyController(clock, timedelta(0))
    evidence = SafetyEvidence(True, True, True, True, True, True, True, True)
    safety.observe(evidence)
    safety.observe(evidence)
    service = ExecutionService(
        broker=broker, quotes=_Quote(quote),
        risk_engine=RiskEngine(load_config().risk, clock, live_capital_ceiling=Decimal("25")),
        store=store, clock=clock, safety=safety, environment=environment,
        namespace="cancellation-test", trading_mode=TradingMode.AUTO,
        cancellation_audit_timeout_seconds=0.03,
    )
    return service, broker, store, safety, trade


@pytest.mark.asyncio
@pytest.mark.parametrize("environment", list(ExecutionEnvironment))
@pytest.mark.parametrize("action", ["place", "cancel"])
async def test_cancelled_write_is_unknown_and_cancellation_propagates(
    clock: VirtualClock, proposal: TradeProposal, environment: ExecutionEnvironment, action: str
) -> None:
    service, broker, store, safety, trade = _service(clock, proposal, environment, action)
    if action == "place":
        task = asyncio.create_task(service.execute(trade))
    else:
        placed = await service.execute(trade)
        task = asyncio.create_task(service.cancel_order(placed.broker_order))  # type: ignore[assignment]
    await asyncio.wait_for(broker.entered.wait(), timeout=1)
    task.cancel("original cancellation")
    with pytest.raises(asyncio.CancelledError, match="original cancellation"):
        await task
    interrupted_id = next(iter(service.interrupted_write_intents))
    assert store.orders[interrupted_id].state is OrderState.SUBMISSION_UNKNOWN
    transition = store.transitions[-1]
    assert transition.intent_id == interrupted_id
    assert transition.previous is OrderState.SUBMITTING
    assert transition.current is OrderState.SUBMISSION_UNKNOWN
    assert "task cancelled during broker" in transition.reason
    assert safety.state is RuntimeSafetyState.HALTED
    calls = list(broker.calls)
    changed = trade.model_copy(update={"proposal_id": uuid4(), "limit_price": Decimal("0.09")})
    # Even a mistaken reuse of old health evidence cannot clear this service latch.
    evidence = SafetyEvidence(True, True, True, True, True, True, True, True)
    safety.observe(evidence, manual_halt_cleared=True)
    safety.observe(evidence, manual_halt_cleared=True)
    with pytest.raises(ExecutionDenied, match="interrupted write latch"):
        await service.execute(changed)
    assert broker.calls == calls
    assert not hasattr(service, "replace_order")


@pytest.mark.asyncio
@pytest.mark.parametrize("action", ["place", "cancel"])
@pytest.mark.parametrize("failure", ["save", "transition", "timeout", "second_cancel"])
async def test_failed_cancellation_audit_keeps_durable_unresolved_state_and_halts(
    clock: VirtualClock, proposal: TradeProposal, action: str, failure: str
) -> None:
    service, broker, store, safety, trade = _service(
        clock, proposal, ExecutionEnvironment.LIVE, action, failure
    )
    if action == "place":
        task = asyncio.create_task(service.execute(trade))
    else:
        placed = await service.execute(trade)
        task = asyncio.create_task(service.cancel_order(placed.broker_order))  # type: ignore[assignment]
    await broker.entered.wait()
    task.cancel("original cancellation")
    if failure == "second_cancel":
        await store.audit_entered.wait()
        task.cancel("second cancellation")
    with pytest.raises(asyncio.CancelledError, match="original cancellation"):
        await asyncio.wait_for(task, timeout=0.3)
    interrupted_id = next(iter(service.interrupted_write_intents))
    expected = OrderState.SUBMISSION_UNKNOWN if failure == "transition" else OrderState.SUBMITTING
    assert store.orders[interrupted_id].state is expected
    assert interrupted_id in store.order_intents
    assert await store.get_command_for_order_intent(interrupted_id) is not None
    assert safety.state is RuntimeSafetyState.HALTED
    assert "sensitive" not in safety.reason
    assert broker.calls.count(action) == 1


@pytest.mark.asyncio
async def test_cancellation_before_write_does_not_claim_broker_submission(
    clock: VirtualClock, proposal: TradeProposal
) -> None:
    service, broker, store, _, trade = _service(
        clock, proposal, ExecutionEnvironment.DEMO, "review"
    )
    task = asyncio.create_task(service.execute(trade))
    await broker.entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert not store.order_intents
    assert not service.interrupted_write_intents
    assert not broker.calls


@pytest.mark.asyncio
async def test_queued_proposal_cannot_enter_broker_after_cancelled_command(
    clock: VirtualClock, proposal: TradeProposal
) -> None:
    service, broker, _, safety, trade = _service(
        clock, proposal, ExecutionEnvironment.LIVE, "place", "timeout"
    )
    first = asyncio.create_task(service.execute(trade))
    await broker.entered.wait()
    changed = trade.model_copy(update={"proposal_id": uuid4(), "limit_price": Decimal("0.09")})
    queued = asyncio.create_task(service.execute(changed))
    first.cancel()
    with pytest.raises(asyncio.CancelledError):
        await first
    with pytest.raises(ExecutionDenied, match="interrupted write latch"):
        await queued
    assert broker.calls == ["place"]
    assert safety.state is RuntimeSafetyState.HALTED
