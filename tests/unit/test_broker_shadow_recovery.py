from __future__ import annotations

from datetime import timedelta
from decimal import Decimal
from typing import Any
from uuid import uuid4

import pytest

from app.broker import ConservativeFillModel, LedgerSnapshot, RobinhoodShadowBroker, SimulatedBroker
from app.broker.base import BrokerCapabilities, validate_command_for_capability
from app.clock.base import VirtualClock
from app.domain.enums import AccountKind, BrokerAction, ExecutionEnvironment, OrderState
from app.domain.models import (
    AccountSnapshot,
    BrokerCommandIntent,
    BrokerOrder,
    BrokerReview,
    FirewallDecision,
    OptionQuote,
    Position,
    ProviderMetadata,
    TradeProposal,
)
from app.exceptions import SafetyCriticalError
from app.safety.write_firewall import DenyAllWriteFirewall


class ReadClient:
    """Capability-specific mock with no generic call or external write surface."""

    def __init__(self, clock: VirtualClock) -> None:
        self.clock = clock
        self._schemas = SimulatedBroker(clock=clock)
        self.account = AccountSnapshot(
            created_at=clock.now(),
            environment=ExecutionEnvironment.DEMO,
            account_kind=AccountKind.BROKER_OBSERVED,
            account_fingerprint="expected-agentic",
            cash=Decimal("0"),
            buying_power=Decimal("0"),
            as_of=clock.now(),
            is_authenticated=True,
            state_known=True,
        )
        self.positions: tuple[Position, ...] = ()
        self.orders: tuple[BrokerOrder, ...] = ()

    async def get_capabilities(self) -> BrokerCapabilities:
        return await self._schemas.get_capabilities()

    async def get_account_state(self) -> AccountSnapshot:
        return self.account

    async def get_positions(self) -> tuple[Position, ...]:
        return self.positions

    async def get_orders(self) -> tuple[BrokerOrder, ...]:
        return self.orders

    async def review_option_order(self, proposal: TradeProposal) -> BrokerReview:
        return BrokerReview(
            created_at=self.clock.now(),
            environment=ExecutionEnvironment.DEMO,
            proposal_id=proposal.proposal_id,
            accepted=False,
            warnings=("real account has no cash",),
            side_effect_free=True,
        )

    async def validate_command(self, command: BrokerCommandIntent) -> dict[str, Any]:
        return validate_command_for_capability(command, await self.get_capabilities())


def market(clock: VirtualClock, proposal: TradeProposal, *, ask: str = "0.08") -> OptionQuote:
    return OptionQuote(
        contract=proposal.contract,
        bid=Decimal("0.06"),
        ask=Decimal(ask),
        bid_size=2,
        ask_size=2,
        volume=100,
        open_interest=500,
        metadata=ProviderMetadata(
            provider="test-fixture",
            capability_version="v1",
            observed_at=clock.now(),
            effective_at=clock.now(),
        ),
    )


def broker_for(
    clock: VirtualClock, proposal: TradeProposal, **kwargs: Any
) -> RobinhoodShadowBroker:
    return RobinhoodShadowBroker(
        read_client=kwargs.pop("read_client", ReadClient(clock)),
        clock=clock,
        namespace=proposal.namespace,
        fill_seed=1729,
        expected_account_fingerprint="expected-agentic",
        **kwargs,
    )


async def command_for(
    broker: RobinhoodShadowBroker, proposal: TradeProposal, *, target: BrokerOrder | None = None
) -> BrokerCommandIntent:
    action = BrokerAction.CANCEL_OPTION_ORDER if target else BrokerAction.PLACE_OPTION_ORDER
    descriptor = (await broker.get_capabilities()).descriptor_for_action(action)
    assert descriptor is not None
    key = str(uuid4())
    arguments = (
        {"order_id": str(target.order_id), "client_order_id": key}
        if target
        else {
            "instrument_id": proposal.contract.instrument_id,
            "side": proposal.side.value,
            "quantity": proposal.quantity,
            "limit_price": str(proposal.limit_price),
            "time_in_force": "day",
            "client_order_id": key,
        }
    )
    return BrokerCommandIntent(
        created_at=proposal.created_at,
        order_intent_id=uuid4(),
        environment=ExecutionEnvironment.DEMO,
        namespace=proposal.namespace,
        action=action,
        capability_name=descriptor.tool_name,
        capability_schema_version=descriptor.schema_version,
        capability_schema_hash=descriptor.schema_hash,
        instrument_id=proposal.contract.instrument_id,
        side=proposal.side,
        quantity=proposal.quantity,
        limit_price=proposal.limit_price,
        validated_arguments=arguments,
        proposal_id=proposal.proposal_id,
        risk_decision_id=uuid4(),
        approval_id=None,
        quote_snapshot_id=proposal.quote_snapshot_id,
        broker_observed_account_snapshot_id=(
            await broker.get_observed_broker_account_state()
        ).snapshot_id,
        effective_account_snapshot_id=(
            await broker.get_effective_execution_account_state()
        ).snapshot_id,
        policy_version=proposal.policy_version,
        order_fingerprint=proposal.order_fingerprint,
        idempotency_key=key,
    )


async def place(
    broker: RobinhoodShadowBroker, clock: VirtualClock, proposal: TradeProposal
) -> tuple[BrokerOrder, BrokerCommandIntent]:
    await broker.consume_quote(market(clock, proposal))
    await broker.review_option_order(proposal)
    command = await command_for(broker, proposal)
    order = await broker.place_option_order(command, proposal.contract)
    return order, command


async def test_open_order_restart_preserves_exact_command_and_deterministic_fill(
    clock: VirtualClock,
    proposal: TradeProposal,
) -> None:
    original = broker_for(clock, proposal)
    order, command = await place(original, clock, proposal)
    encoded = original.export_state().model_dump_json()
    decoded = LedgerSnapshot.model_validate_json(encoded)
    recorded: list[BrokerCommandIntent] = []
    restored = broker_for(clock, proposal, initial_state=decoded, command_recorder=recorded.append)
    assert await restored.get_orders() == (order,)
    assert restored.recorded_command_intents == (command,)
    assert await restored.place_option_order(command, proposal.contract) == order
    assert recorded == []
    await clock.advance(timedelta(seconds=1))
    quote = market(clock, proposal, ask="0.07")
    original_fills = await original.consume_quote(quote)
    restored_fills = await restored.consume_quote(quote)
    assert original_fills[0].deterministic_seed == restored_fills[0].deterministic_seed
    assert original.export_state().cash == restored.export_state().cash
    assert (await restored.reconcile()).successful


async def test_cancel_restart_retains_idempotency_and_rejects_changed_command(
    clock: VirtualClock,
    proposal: TradeProposal,
) -> None:
    original = broker_for(clock, proposal)
    order, placed_command = await place(original, clock, proposal)
    cancel = await command_for(original, proposal, target=order)
    canceled = await original.cancel_option_order(cancel, order.order_id)
    restored = broker_for(clock, proposal, initial_state=original.export_state())
    assert restored.recorded_command_intents == (placed_command, cancel)
    assert await restored.cancel_option_order(cancel, order.order_id) == canceled
    with pytest.raises(SafetyCriticalError, match="different content"):
        await restored.cancel_option_order(
            cancel.model_copy(update={"limit_price": Decimal("0.07")}), order.order_id
        )


async def test_partial_fill_restart_cannot_reuse_quote_liquidity(
    clock: VirtualClock,
    proposal: TradeProposal,
) -> None:
    proposal = proposal.model_copy(update={"quantity": 3})
    fill_model = ConservativeFillModel(
        touch_fill_probability=Decimal("0"), displayed_size_participation=Decimal("0.5")
    )
    original = broker_for(clock, proposal, fill_model=fill_model)
    await place(original, clock, proposal)
    await clock.advance(timedelta(seconds=1))
    quote = market(clock, proposal, ask="0.07")
    assert len(await original.consume_quote(quote)) == 1
    restored = broker_for(
        clock, proposal, fill_model=fill_model, initial_state=original.export_state()
    )
    assert await restored.consume_quote(quote) == ()
    assert (await restored.get_orders())[0].filled_quantity == 1


@pytest.mark.parametrize("mutation", ["review", "place", "cancel", "quote", "deposit", "expiry"])
async def test_failed_persistence_blocks_all_mutations_until_explicit_flush(
    clock: VirtualClock,
    proposal: TradeProposal,
    mutation: str,
) -> None:
    writable = True
    snapshots: list[LedgerSnapshot] = []

    async def persist(snapshot: LedgerSnapshot) -> None:
        if not writable:
            raise OSError("database unavailable")
        snapshots.append(snapshot)

    broker = broker_for(clock, proposal, state_recorder=persist)
    order, command = await place(broker, clock, proposal)
    cancel = await command_for(broker, proposal, target=order)
    writable = False
    with pytest.raises(OSError, match="database unavailable"):
        await broker.deposit(Decimal("1"))
    assert not broker.state_persisted
    assert not (await broker.get_capabilities()).execution_ready
    assert not (await broker.reconcile()).successful
    with pytest.raises(SafetyCriticalError, match="unpersisted mutation"):
        if mutation == "review":
            await broker.review_option_order(proposal)
        elif mutation == "place":
            await broker.place_option_order(command, proposal.contract)
        elif mutation == "cancel":
            await broker.cancel_option_order(cancel, order.order_id)
        elif mutation == "quote":
            await broker.consume_quote(market(clock, proposal))
        elif mutation == "deposit":
            await broker.deposit(Decimal("2"))
        else:
            await broker.process_expirations()
    writable = True
    await broker.flush_state()
    assert broker.state_persisted
    assert snapshots[-1].cash == Decimal("26")
    assert (await broker.reconcile()).successful
    assert (await broker.get_capabilities()).execution_ready


async def test_firewall_denial_precedes_command_recording_and_ledger_persistence(
    clock: VirtualClock,
    proposal: TradeProposal,
) -> None:
    sequence: list[str] = []
    firewall = DenyAllWriteFirewall(clock, lambda _: sequence.append("denied"))
    broker = broker_for(
        clock,
        proposal,
        firewall=firewall,
        command_recorder=lambda _: sequence.append("command"),
        state_recorder=lambda _: sequence.append("ledger"),
    )
    await broker.consume_quote(market(clock, proposal))
    command = await command_for(broker, proposal)
    sequence.clear()
    await broker.place_option_order(command, proposal.contract)
    assert sequence == ["denied", "command", "ledger"]
    assert not hasattr(broker, "_write_transport")


class WrongDecisionFirewall(DenyAllWriteFirewall):
    async def evaluate(self, command: BrokerCommandIntent) -> FirewallDecision:
        return (await super().evaluate(command)).model_copy(update={"command_intent_id": uuid4()})


async def test_wrong_firewall_decision_cannot_record_or_apply_a_local_command(
    clock: VirtualClock,
    proposal: TradeProposal,
) -> None:
    commands: list[BrokerCommandIntent] = []
    broker = broker_for(
        clock, proposal, firewall=WrongDecisionFirewall(clock), command_recorder=commands.append
    )
    await broker.consume_quote(market(clock, proposal))
    command = await command_for(broker, proposal)
    with pytest.raises(SafetyCriticalError, match="exact command"):
        await broker.place_option_order(command, proposal.contract)
    assert await broker.get_orders() == ()
    assert broker.recorded_command_intents == ()
    assert commands == []


@pytest.mark.parametrize("change", ["missing", "duplicate", "wrong_hash", "missing_key"])
async def test_restore_rejects_incomplete_or_ambiguous_command_audit(
    clock: VirtualClock,
    proposal: TradeProposal,
    change: str,
) -> None:
    broker = broker_for(clock, proposal)
    _, command = await place(broker, clock, proposal)
    snapshot = broker.export_state()
    if change == "missing":
        snapshot = snapshot.model_copy(update={"recorded_commands": ()})
    elif change == "duplicate":
        snapshot = snapshot.model_copy(update={"recorded_commands": (command, command)})
    elif change == "wrong_hash":
        snapshot = snapshot.model_copy(
            update={
                "recorded_commands": (command.model_copy(update={"policy_version": "changed"}),)
            }
        )
    else:
        snapshot = snapshot.model_copy(update={"idempotency_order_ids": {}})
    with pytest.raises(SafetyCriticalError):
        broker_for(clock, proposal, initial_state=snapshot)


@pytest.mark.parametrize("state", list(OrderState))
async def test_only_acknowledged_terminal_history_can_be_excluded_from_active_orders(
    clock: VirtualClock,
    proposal: TradeProposal,
    state: OrderState,
) -> None:
    original = broker_for(clock, proposal)
    order, _ = await place(original, clock, proposal)
    read_client = ReadClient(clock)
    read_client.orders = (
        order.model_copy(update={"state": state, "broker_order_id": "historical-id"}),
    )
    broker = broker_for(
        clock,
        proposal,
        read_client=read_client,
        acknowledged_historical_order_ids=frozenset({"historical-id"}),
    )
    report = await broker.reconcile()
    terminal = state in {
        OrderState.FILLED,
        OrderState.CANCELED,
        OrderState.REJECTED,
        OrderState.EXPIRED,
    }
    assert report.successful is terminal
    assert report.details["observed_order_count"] == 1
    assert report.details["observed_historical_order_count"] == int(terminal)
    assert report.details["observed_active_order_count"] == int(not terminal)


async def test_unacknowledged_terminal_order_remains_a_qualification_failure(
    clock: VirtualClock,
    proposal: TradeProposal,
) -> None:
    original = broker_for(clock, proposal)
    order, _ = await place(original, clock, proposal)
    read_client = ReadClient(clock)
    read_client.orders = (
        order.model_copy(update={"state": OrderState.CANCELED, "broker_order_id": "unknown"}),
    )
    report = await broker_for(clock, proposal, read_client=read_client).reconcile()
    assert not report.successful
    assert report.details["unacknowledged_historical_order_count"] == 1


@pytest.mark.parametrize("field", ["cash", "buying_power"])
async def test_unexpected_funds_still_fail_qualification(
    clock: VirtualClock,
    proposal: TradeProposal,
    field: str,
) -> None:
    read_client = ReadClient(clock)
    read_client.account = read_client.account.model_copy(update={field: Decimal("1")})
    assert not (await broker_for(clock, proposal, read_client=read_client).reconcile()).successful


async def test_selected_account_mismatch_cannot_be_reconciled(
    clock: VirtualClock,
    proposal: TradeProposal,
) -> None:
    read_client = ReadClient(clock)
    read_client.account = read_client.account.model_copy(
        update={"account_fingerprint": "different"}
    )
    with pytest.raises(SafetyCriticalError, match="different selected account"):
        await broker_for(clock, proposal, read_client=read_client).reconcile()


async def test_failed_order_snapshot_can_be_flushed_and_restored_without_resubmitting(
    clock: VirtualClock,
    proposal: TradeProposal,
) -> None:
    writable = True
    snapshots: list[LedgerSnapshot] = []

    def persist(snapshot: LedgerSnapshot) -> None:
        if not writable:
            raise OSError("snapshot failed")
        snapshots.append(snapshot)

    broker = broker_for(clock, proposal, state_recorder=persist)
    await broker.consume_quote(market(clock, proposal))
    command = await command_for(broker, proposal)
    writable = False
    with pytest.raises(OSError, match="snapshot failed"):
        await broker.place_option_order(command, proposal.contract)
    assert len(await broker.get_orders()) == 1
    assert not broker.state_persisted
    writable = True
    await broker.flush_state()
    restored = broker_for(clock, proposal, initial_state=snapshots[-1])
    original_order = (await broker.get_orders())[0]
    assert await restored.place_option_order(command, proposal.contract) == original_order
    assert len(await restored.get_orders()) == 1


@pytest.mark.parametrize(
    "changes",
    [
        {"account_kind": AccountKind.SIMULATED},
        {"environment": ExecutionEnvironment.LIVE},
        {"namespace": "another-ledger"},
        {"initial_cash": Decimal("100")},
        {"fill_model_version": "another-fill-model"},
    ],
)
def test_snapshot_must_match_immutable_shadow_binding(
    clock: VirtualClock,
    proposal: TradeProposal,
    changes: dict[str, Any],
) -> None:
    snapshot = broker_for(clock, proposal).export_state().model_copy(update=changes)
    with pytest.raises(SafetyCriticalError):
        broker_for(clock, proposal, initial_state=snapshot)


@pytest.mark.parametrize(
    "changes",
    [{"namespace": "other"}, {"environment": ExecutionEnvironment.LIVE}],
)
async def test_cross_boundary_cancel_cannot_mutate_or_record(
    clock: VirtualClock,
    proposal: TradeProposal,
    changes: dict[str, Any],
) -> None:
    broker = broker_for(clock, proposal)
    order, placed_command = await place(broker, clock, proposal)
    cancel = (await command_for(broker, proposal, target=order)).model_copy(update=changes)
    with pytest.raises(SafetyCriticalError, match="immutable runtime binding"):
        await broker.cancel_option_order(cancel, order.order_id)
    assert broker.recorded_command_intents == (placed_command,)
    assert (await broker.get_orders())[0].state is OrderState.OPEN


async def test_unexpected_real_position_still_blocks_qualification(
    clock: VirtualClock,
    proposal: TradeProposal,
) -> None:
    original = broker_for(clock, proposal)
    await place(original, clock, proposal)
    await clock.advance(timedelta(seconds=1))
    await original.consume_quote(market(clock, proposal, ask="0.07"))
    read_client = ReadClient(clock)
    read_client.positions = await original.get_positions()
    report = await broker_for(clock, proposal, read_client=read_client).reconcile()
    assert not report.successful
    assert report.details["observed_position_count"] == 1


@pytest.mark.parametrize(
    "changes",
    [
        {"account_fingerprint": None},
        {"account_kind": AccountKind.SIMULATED},
        {"environment": ExecutionEnvironment.LIVE},
    ],
)
async def test_read_client_account_view_must_have_explicit_shadow_identity(
    clock: VirtualClock,
    proposal: TradeProposal,
    changes: dict[str, Any],
) -> None:
    read_client = ReadClient(clock)
    read_client.account = read_client.account.model_copy(update=changes)
    with pytest.raises(SafetyCriticalError):
        await broker_for(
            clock, proposal, read_client=read_client
        ).get_observed_broker_account_state()
