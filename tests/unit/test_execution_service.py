from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import pytest

from app.broker.simulated import SimulatedBroker
from app.clock.base import VirtualClock
from app.config import load_config
from app.domain.enums import ExecutionEnvironment, OptionType, OrderSide, OrderState, TradingMode
from app.domain.models import (
    ExactApproval,
    OptionContract,
    OptionQuote,
    ProviderMetadata,
    TradeProposal,
)
from app.execution import ExecutionDenied, ExecutionService, InMemoryExecutionStore
from app.risk import RiskEngine
from app.safety.runtime_state import SafetyController, SafetyEvidence

NOW = datetime(2026, 9, 2, 15, 0, tzinfo=UTC)


class Quotes:
    def __init__(self, value: OptionQuote) -> None:
        self.value = value

    async def get_option_quote(self, instrument_id: str) -> OptionQuote:
        assert instrument_id == self.value.contract.instrument_id
        return self.value


def market() -> OptionQuote:
    return OptionQuote(
        contract=OptionContract(
            instrument_id="opt-1",
            symbol="TEST",
            option_type=OptionType.CALL,
            strike=Decimal("10"),
            expiration=date(2026, 9, 18),
        ),
        bid=Decimal("0.07"),
        ask=Decimal("0.08"),
        last=Decimal("0.08"),
        volume=100,
        open_interest=500,
        bid_size=10,
        ask_size=10,
        metadata=ProviderMetadata(
            provider="fixture",
            capability_version="v1",
            observed_at=NOW,
            effective_at=NOW,
        ),
    )


def proposal(quote: OptionQuote) -> TradeProposal:
    return TradeProposal(
        created_at=NOW,
        environment=ExecutionEnvironment.DEMO,
        namespace="demo-test",
        packet_id="00000000-0000-0000-0000-000000000001",
        symbol="TEST",
        contract=quote.contract,
        side=OrderSide.BUY_TO_OPEN,
        quantity=1,
        limit_price=Decimal("0.08"),
        quote_snapshot_id=quote.snapshot_id,
        quote_as_of=NOW,
        policy_version="demo-exploratory-v1",
        risk_config_version="risk-v1",
        thesis="fixture",
        invalidation_conditions=("fixture invalidated",),
    )


def normal_safety(clock: VirtualClock) -> SafetyController:
    controller = SafetyController(clock, timedelta(0))
    evidence = SafetyEvidence(
        database_writable=True,
        broker_state_known=True,
        reconciled=True,
        market_data_fresh=True,
        account_data_fresh=True,
        execution_service_healthy=True,
        kill_switch_clear=True,
        environment_matches=True,
    )
    controller.observe(evidence)
    controller.observe(evidence)
    return controller


@pytest.mark.asyncio
async def test_exact_intents_are_durable_before_simulated_submission() -> None:
    quote = market()
    clock = VirtualClock(NOW)
    broker = SimulatedBroker(clock=clock, namespace="demo-test")
    await broker.consume_quote(quote)
    store = InMemoryExecutionStore()
    service = ExecutionService(
        broker=broker,
        quotes=Quotes(quote),
        risk_engine=RiskEngine(load_config().risk, clock),
        store=store,
        clock=clock,
        safety=normal_safety(clock),
        environment=ExecutionEnvironment.DEMO,
        namespace="demo-test",
        trading_mode=TradingMode.AUTO,
    )

    result = await service.execute_entry(proposal(quote))

    assert result.broker_order.state is OrderState.OPEN
    kinds = [kind for kind, _ in store.audit_sequence]
    assert kinds.index("order_intent") < kinds.index("broker_command_intent")
    assert kinds.index("broker_command_intent") < kinds.index("broker_order")
    assert broker.recorded_command_intents == (result.command_intent,)
    assert result.command_intent.validated_arguments["instrument_id"] == "opt-1"

    # Reconstructing the same exact proposal produces stable simulated order
    # identity and therefore stable conservative fill seeds.
    second_broker = SimulatedBroker(clock=clock, namespace="demo-test")
    await second_broker.consume_quote(quote)
    second = await ExecutionService(
        broker=second_broker,
        quotes=Quotes(quote),
        risk_engine=RiskEngine(load_config().risk, clock),
        store=InMemoryExecutionStore(),
        clock=clock,
        safety=normal_safety(clock),
        environment=ExecutionEnvironment.DEMO,
        namespace="demo-test",
        trading_mode=TradingMode.AUTO,
    ).execute_entry(
        proposal(quote).model_copy(update={"proposal_id": result.order_intent.proposal_id})
    )
    assert second.order_intent.intent_id == result.order_intent.intent_id
    assert second.broker_order.order_id == result.broker_order.order_id


@pytest.mark.asyncio
async def test_approval_is_exact_not_ticker_wide() -> None:
    quote = market()
    trade = proposal(quote)
    clock = VirtualClock(NOW)
    broker = SimulatedBroker(clock=clock, namespace="demo-test")
    await broker.consume_quote(quote)
    store = InMemoryExecutionStore()
    service = ExecutionService(
        broker=broker,
        quotes=Quotes(quote),
        risk_engine=RiskEngine(load_config().risk, clock),
        store=store,
        clock=clock,
        safety=normal_safety(clock),
        environment=ExecutionEnvironment.DEMO,
        namespace="demo-test",
        trading_mode=TradingMode.APPROVAL,
    )
    wrong = ExactApproval(
        created_at=NOW,
        environment=ExecutionEnvironment.DEMO,
        namespace="demo-test",
        proposal_id=trade.proposal_id,
        order_fingerprint="different-order",
        maximum_limit_price=Decimal("1"),
        expires_at=NOW + timedelta(minutes=5),
        approved_by="tester",
    )

    with pytest.raises(ExecutionDenied, match="non-exact"):
        await service.execute_entry(trade, approval=wrong)

    assert not store.order_intents
    assert not broker.recorded_command_intents


@pytest.mark.asyncio
async def test_duplicate_fingerprint_never_calls_broker_twice() -> None:
    quote = market()
    trade = proposal(quote)
    clock = VirtualClock(NOW)
    broker = SimulatedBroker(clock=clock, namespace="demo-test")
    await broker.consume_quote(quote)
    store = InMemoryExecutionStore()
    service = ExecutionService(
        broker=broker,
        quotes=Quotes(quote),
        risk_engine=RiskEngine(load_config().risk, clock),
        store=store,
        clock=clock,
        safety=normal_safety(clock),
        environment=ExecutionEnvironment.DEMO,
        namespace="demo-test",
        trading_mode=TradingMode.AUTO,
    )

    await service.execute_entry(trade)
    with pytest.raises(ExecutionDenied, match="duplicate"):
        await service.execute_entry(trade)

    assert len(broker.recorded_command_intents) == 1


@pytest.mark.asyncio
async def test_cancellation_has_its_own_durable_exact_command() -> None:
    quote = market()
    clock = VirtualClock(NOW)
    broker = SimulatedBroker(clock=clock, namespace="demo-test")
    await broker.consume_quote(quote)
    store = InMemoryExecutionStore()
    service = ExecutionService(
        broker=broker,
        quotes=Quotes(quote),
        risk_engine=RiskEngine(load_config().risk, clock),
        store=store,
        clock=clock,
        safety=normal_safety(clock),
        environment=ExecutionEnvironment.DEMO,
        namespace="demo-test",
        trading_mode=TradingMode.AUTO,
    )
    placed = await service.execute_entry(proposal(quote))

    canceled = await service.cancel_order(placed.broker_order)

    assert canceled.broker_order.state is OrderState.CANCELED
    assert canceled.order_intent.action.value == "cancel_option_order"
    assert canceled.command_intent.validated_arguments["order_id"] == str(
        placed.broker_order.order_id
    )
    assert len(broker.recorded_command_intents) == 2
