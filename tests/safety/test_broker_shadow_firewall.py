from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any
from uuid import uuid4

import pytest

from app.broker import RobinhoodReadOnlyMcpClient, RobinhoodShadowBroker
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
from app.safety.write_firewall import DenyAllWriteFirewall

NOW = datetime(2026, 9, 2, 15, 0, tzinfo=UTC)
PLACE_SCHEMA = {
    "type": "object",
    "properties": {
        "instrument_id": {"type": "string"},
        "side": {"type": "string"},
        "quantity": {"type": "integer"},
        "limit_price": {"type": "string"},
        "time_in_force": {"type": "string"},
    },
    "required": ["instrument_id", "side", "quantity", "limit_price", "time_in_force"],
    "additionalProperties": False,
}
CANCEL_SCHEMA = {
    "type": "object",
    "properties": {"order_id": {"type": "string"}},
    "required": ["order_id"],
    "additionalProperties": False,
}


class RecordingTransport:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def list_tools(self) -> dict[str, object]:
        schemas = {
            "get_account_state": {"type": "object", "additionalProperties": False},
            "get_option_positions": {"type": "object", "additionalProperties": False},
            "get_option_orders": {"type": "object", "additionalProperties": False},
            "review_option_order": PLACE_SCHEMA,
            "place_option_order": PLACE_SCHEMA,
            "cancel_option_order": CANCEL_SCHEMA,
        }
        return {
            "tools": [
                {
                    "name": name,
                    "inputSchema": schema,
                    "annotations": {"destructiveHint": "place" in name or "cancel" in name},
                }
                for name, schema in schemas.items()
            ]
        }

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> object:
        self.calls.append((name, arguments))
        if name == "get_account_state":
            # Nonzero funds deliberately prove that cash is not the write boundary.
            return {"cash": "1000", "buying_power": "1000", "account_id": "secret"}
        if name in {"get_option_positions", "get_option_orders"}:
            return []
        if name == "review_option_order":
            return {"accepted": True, "warnings": []}
        raise AssertionError(f"mutation reached network transport: {name}")


def market() -> OptionQuote:
    contract = OptionContract(
        instrument_id="opt-shadow",
        symbol="TEST",
        option_type=OptionType.CALL,
        strike=Decimal("10"),
        expiration=date(2026, 9, 18),
    )
    return OptionQuote(
        contract=contract,
        bid=Decimal("0.07"),
        ask=Decimal("0.08"),
        volume=100,
        open_interest=500,
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
        namespace="shadow-test",
        packet_id=uuid4(),
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
        invalidation_conditions=("invalid",),
    )


async def place_command(broker: RobinhoodShadowBroker, trade: TradeProposal) -> BrokerCommandIntent:
    capability = (await broker.get_capabilities()).descriptor_for_action(
        BrokerAction.PLACE_OPTION_ORDER
    )
    assert capability is not None
    arguments = {
        "instrument_id": trade.contract.instrument_id,
        "side": trade.side.value,
        "quantity": trade.quantity,
        "limit_price": str(trade.limit_price),
        "time_in_force": "day",
    }
    shadow_account = await broker.get_effective_execution_account_state()
    observed = await broker.get_observed_broker_account_state()
    return BrokerCommandIntent(
        created_at=NOW,
        order_intent_id=uuid4(),
        environment=ExecutionEnvironment.DEMO,
        namespace="shadow-test",
        action=BrokerAction.PLACE_OPTION_ORDER,
        capability_name=capability.tool_name,
        capability_schema_version=capability.schema_version,
        capability_schema_hash=capability.schema_hash,
        instrument_id=trade.contract.instrument_id,
        side=trade.side,
        quantity=trade.quantity,
        limit_price=trade.limit_price,
        validated_arguments=arguments,
        proposal_id=trade.proposal_id,
        risk_decision_id=uuid4(),
        approval_id=None,
        quote_snapshot_id=trade.quote_snapshot_id,
        broker_observed_account_snapshot_id=observed.snapshot_id,
        effective_account_snapshot_id=shadow_account.snapshot_id,
        policy_version=trade.policy_version,
        order_fingerprint=trade.order_fingerprint,
        idempotency_key="shadow-place-1",
    )


@pytest.mark.asyncio
async def test_shadow_auto_path_with_real_funds_cannot_transmit_mutation() -> None:
    transport = RecordingTransport()
    clock = VirtualClock(NOW)
    read_client = RobinhoodReadOnlyMcpClient(transport=transport, clock=clock)
    firewall_events = []
    firewall = DenyAllWriteFirewall(clock, firewall_events.append)
    broker = RobinhoodShadowBroker(
        read_client=read_client,
        clock=clock,
        namespace="shadow-test",
        firewall=firewall,
    )
    quote = market()
    await broker.consume_quote(quote)
    trade = proposal(quote)
    assert (await broker.review_option_order(trade)).accepted
    command = await place_command(broker, trade)

    placed = await broker.place_option_order(command, trade.contract)

    assert placed.state is OrderState.OPEN
    assert firewall_events and not firewall_events[0].transmitted
    names = [name for name, _ in transport.calls]
    assert "place_option_order" not in names
    assert "cancel_option_order" not in names
    assert not hasattr(broker, "_write_transport")
    assert not hasattr(read_client, "call_tool")
    assert (await broker.get_observed_broker_account_state()).cash == Decimal("1000")
    assert (await broker.get_effective_execution_account_state()).cash == Decimal("25")


@pytest.mark.asyncio
async def test_read_facade_default_denies_unknown_or_mutating_capabilities() -> None:
    client = RobinhoodReadOnlyMcpClient(transport=RecordingTransport(), clock=VirtualClock(NOW))
    await client.get_capabilities()

    with pytest.raises(SafetyCriticalError, match="not read/review allowlisted"):
        await client._call_allowed("create_watchlist", {})
