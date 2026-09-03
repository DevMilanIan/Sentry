from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from uuid import uuid4

import pytest

from app.broker import ConservativeFillModel, ExpirationPolicy, LedgerSnapshot, SimulatedBroker
from app.clock.base import VirtualClock
from app.domain.enums import BrokerAction, ExecutionEnvironment, OptionType, OrderSide, OrderState
from app.domain.models import (
    BrokerCommandIntent,
    OptionContract,
    OptionQuote,
    ProviderMetadata,
    TradeProposal,
)
from app.exceptions import SafetyCriticalError

NOW = datetime(2026, 9, 2, 15, 0, tzinfo=UTC)
CONTRACT = OptionContract(
    instrument_id="opt-1",
    symbol="TEST",
    option_type=OptionType.CALL,
    strike=Decimal("10"),
    expiration=date(2026, 9, 18),
)


def quote(at: datetime, *, bid: str = "0.06", ask: str = "0.08", size: int = 2) -> OptionQuote:
    return OptionQuote(
        contract=CONTRACT,
        bid=Decimal(bid),
        ask=Decimal(ask),
        last=Decimal(ask),
        volume=100,
        open_interest=500,
        bid_size=size,
        ask_size=size,
        metadata=ProviderMetadata(
            provider="fixture",
            capability_version="v1",
            observed_at=at,
            effective_at=at,
        ),
    )


def proposal(market: OptionQuote, *, quantity: int = 1) -> TradeProposal:
    return TradeProposal(
        created_at=NOW,
        environment=ExecutionEnvironment.DEMO,
        namespace="demo-test",
        packet_id=uuid4(),
        symbol="TEST",
        contract=CONTRACT,
        side=OrderSide.BUY_TO_OPEN,
        quantity=quantity,
        limit_price=Decimal("0.08"),
        quote_snapshot_id=market.snapshot_id,
        quote_as_of=market.metadata.observed_at,
        policy_version="demo-v1",
        risk_config_version="risk-v1",
        thesis="fixture",
        invalidation_conditions=("invalid",),
    )


async def command(
    broker: SimulatedBroker,
    trade: TradeProposal,
    *,
    action: BrokerAction = BrokerAction.PLACE_OPTION_ORDER,
    key: str = "key-1",
) -> BrokerCommandIntent:
    descriptor = (await broker.get_capabilities()).descriptor_for_action(action)
    assert descriptor is not None
    arguments = (
        {
            "instrument_id": trade.contract.instrument_id,
            "side": trade.side.value,
            "quantity": trade.quantity,
            "limit_price": str(trade.limit_price),
            "time_in_force": "day",
            "client_order_id": key,
        }
        if action is BrokerAction.PLACE_OPTION_ORDER
        else {"order_id": "placeholder", "client_order_id": key}
    )
    return BrokerCommandIntent(
        created_at=NOW,
        order_intent_id=uuid4(),
        environment=ExecutionEnvironment.DEMO,
        namespace="demo-test",
        action=action,
        capability_name=descriptor.tool_name,
        capability_schema_version=descriptor.schema_version,
        capability_schema_hash=descriptor.schema_hash,
        instrument_id=trade.contract.instrument_id,
        side=trade.side,
        quantity=trade.quantity,
        limit_price=trade.limit_price,
        validated_arguments=arguments,
        proposal_id=trade.proposal_id,
        risk_decision_id=uuid4(),
        approval_id=None,
        quote_snapshot_id=trade.quote_snapshot_id,
        broker_observed_account_snapshot_id=None,
        effective_account_snapshot_id=(await broker.get_account_state()).snapshot_id,
        policy_version=trade.policy_version,
        order_fingerprint=trade.order_fingerprint,
        idempotency_key=key,
    )


@pytest.mark.asyncio
async def test_quote_at_submission_time_cannot_fill_and_later_quote_can() -> None:
    clock = VirtualClock(NOW)
    broker = SimulatedBroker(clock=clock, namespace="demo-test")
    initial = quote(NOW)
    await broker.consume_quote(initial)
    trade = proposal(initial)
    assert (await broker.review_option_order(trade)).accepted
    placed = await broker.place_option_order(await command(broker, trade), CONTRACT)

    assert placed.state is OrderState.OPEN
    assert await broker.consume_quote(initial) == ()
    await clock.advance(timedelta(seconds=1))
    fills = await broker.consume_quote(quote(clock.now(), ask="0.07"))

    assert len(fills) == 1
    assert fills[0].market_event_ids
    assert fills[0].fill_model_version.startswith("conservative-queue-v1")
    assert (await broker.get_orders())[0].state is OrderState.FILLED
    assert (await broker.get_positions())[0].quantity == 1
    assert (await broker.get_account_state()).cash == Decimal("18")


@pytest.mark.asyncio
async def test_conservative_size_can_produce_repeatable_partial_fill() -> None:
    clock = VirtualClock(NOW)
    broker = SimulatedBroker(
        clock=clock,
        namespace="demo-test",
        fill_model=ConservativeFillModel(
            touch_fill_probability=Decimal("0"),
            displayed_size_participation=Decimal("0.50"),
        ),
    )
    initial = quote(NOW)
    await broker.consume_quote(initial)
    trade = proposal(initial, quantity=3)
    placed = await broker.place_option_order(await command(broker, trade), CONTRACT)
    await clock.advance(timedelta(seconds=1))

    fills = await broker.consume_quote(quote(clock.now(), ask="0.07", size=2))

    updated = next(item for item in await broker.get_orders() if item.order_id == placed.order_id)
    assert fills[0].quantity == 1
    assert updated.state is OrderState.PARTIAL
    assert updated.filled_quantity == 1


@pytest.mark.asyncio
async def test_expiration_never_creates_underlying_shares_or_negative_cash() -> None:
    clock = VirtualClock(NOW)
    broker = SimulatedBroker(clock=clock, namespace="demo-test")
    initial = quote(NOW)
    await broker.consume_quote(initial)
    trade = proposal(initial)
    await broker.place_option_order(await command(broker, trade), CONTRACT)
    await clock.advance(timedelta(seconds=1))
    await broker.consume_quote(quote(clock.now(), ask="0.07"))
    await clock.advance_to(datetime(2026, 9, 18, 20, 0, tzinfo=UTC))

    result = await broker.process_expirations(
        on_date=CONTRACT.expiration,
        policy=ExpirationPolicy.DO_NOT_EXERCISE,
    )

    assert result.expired_instrument_ids == (CONTRACT.instrument_id,)
    assert await broker.get_positions() == ()
    assert (await broker.get_account_state()).cash >= 0


@pytest.mark.asyncio
async def test_open_order_and_idempotency_survive_ledger_snapshot_restart() -> None:
    clock = VirtualClock(NOW)
    broker = SimulatedBroker(clock=clock, namespace="demo-test", fill_seed=1729)
    initial = quote(NOW)
    await broker.consume_quote(initial)
    trade = proposal(initial)
    await broker.review_option_order(trade)
    exact_command = await command(broker, trade)
    placed = await broker.place_option_order(exact_command, CONTRACT)
    snapshot = broker.export_state()
    decoded = LedgerSnapshot.model_validate_json(snapshot.model_dump_json())
    restored_clock = VirtualClock(NOW)
    restored = SimulatedBroker(
        clock=restored_clock,
        namespace="demo-test",
        fill_seed=1729,
        initial_state=decoded,
    )

    assert decoded.content_hash == snapshot.content_hash
    assert (await restored.get_orders()) == (placed,)
    assert await restored.place_option_order(exact_command, CONTRACT) == placed
    await clock.advance(timedelta(seconds=1))
    await restored_clock.advance(timedelta(seconds=1))
    next_quote = quote(clock.now(), ask="0.07")
    original_fills = await broker.consume_quote(next_quote)
    restored_fills = await restored.consume_quote(next_quote)

    assert original_fills[0].deterministic_seed == restored_fills[0].deterministic_seed
    assert (await restored.get_account_state()).cash == (await broker.get_account_state()).cash
    assert (await restored.get_orders())[0].state is OrderState.FILLED
    assert (await restored.reconcile()).successful


@pytest.mark.asyncio
async def test_partial_fill_restart_does_not_reuse_the_same_quote_liquidity() -> None:
    clock = VirtualClock(NOW)
    fill_model = ConservativeFillModel(
        touch_fill_probability=Decimal("0"),
        displayed_size_participation=Decimal("0.50"),
    )
    broker = SimulatedBroker(clock=clock, namespace="demo-test", fill_model=fill_model)
    initial = quote(NOW)
    await broker.consume_quote(initial)
    trade = proposal(initial, quantity=3)
    await broker.place_option_order(await command(broker, trade), CONTRACT)
    await clock.advance(timedelta(seconds=1))
    partial_quote = quote(clock.now(), ask="0.07", size=2)
    assert len(await broker.consume_quote(partial_quote)) == 1
    restored = SimulatedBroker(
        clock=clock,
        namespace="demo-test",
        fill_model=fill_model,
        initial_state=broker.export_state(),
    )

    assert await restored.consume_quote(partial_quote) == ()
    assert (await restored.get_orders())[0].filled_quantity == 1
    assert (await restored.get_account_state()).cash == Decimal("18")


@pytest.mark.asyncio
async def test_failed_ledger_persistence_blocks_further_mutations_until_flushed() -> None:
    clock = VirtualClock(NOW)
    writable = False
    recorded: list[LedgerSnapshot] = []

    async def persist(snapshot: LedgerSnapshot) -> None:
        if not writable:
            raise OSError("database unavailable")
        recorded.append(snapshot)

    broker = SimulatedBroker(clock=clock, namespace="demo-test", state_recorder=persist)
    with pytest.raises(OSError, match="database unavailable"):
        await broker.consume_quote(quote(NOW))
    assert not broker.state_persisted
    assert not (await broker.reconcile()).successful
    with pytest.raises(SafetyCriticalError, match="unpersisted mutation"):
        await broker.deposit(Decimal("5"))

    writable = True
    await broker.flush_state()
    assert broker.state_persisted
    assert recorded[-1].quotes
    await broker.deposit(Decimal("5"))
    assert (await broker.get_account_state()).cash == Decimal("30")


def test_ledger_restore_rejects_identity_or_fill_model_changes() -> None:
    clock = VirtualClock(NOW)
    snapshot = SimulatedBroker(
        clock=clock, namespace="demo-test", fill_seed=1729
    ).export_state()

    with pytest.raises(SafetyCriticalError, match="identity"):
        SimulatedBroker(clock=clock, namespace="other", fill_seed=1729, initial_state=snapshot)
    with pytest.raises(SafetyCriticalError, match="fill model changed"):
        SimulatedBroker(clock=clock, namespace="demo-test", fill_seed=7, initial_state=snapshot)
