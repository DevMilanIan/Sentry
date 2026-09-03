from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import pytest

from app.broker.base import (
    Broker,
    BrokerCapabilities,
    CapabilityDescriptor,
    ReconciliationReport,
)
from app.clock.base import VirtualClock
from app.config import load_config
from app.domain.enums import (
    AccountKind,
    ExecutionEnvironment,
    OptionType,
    OrderSide,
    OrderState,
    TradingMode,
)
from app.domain.models import (
    AccountSnapshot,
    BrokerCommandIntent,
    BrokerOrder,
    BrokerReview,
    OptionContract,
    OptionQuote,
    Position,
    ProviderMetadata,
    TradeProposal,
)
from app.exceptions import SubmissionUnknownError
from app.execution import ExecutionService, InMemoryExecutionStore
from app.risk import RiskEngine
from app.safety.runtime_state import SafetyController, SafetyEvidence

NOW = datetime(2026, 9, 2, 15, 0, tzinfo=UTC)
PLACE_SCHEMA = {
    "type": "object",
    "properties": {
        "instrument_id": {"type": "string"},
        "side": {"type": "string"},
        "quantity": {"type": "integer"},
        "limit_price": {"type": "string"},
        "time_in_force": {"type": "string"},
        "client_order_id": {"type": "string"},
    },
    "required": ["instrument_id", "side", "quantity", "limit_price", "time_in_force"],
    "additionalProperties": False,
}


class Quotes:
    def __init__(self, quote: OptionQuote) -> None:
        self.quote = quote

    async def get_option_quote(self, instrument_id: str) -> OptionQuote:
        assert instrument_id == self.quote.contract.instrument_id
        return self.quote


class TimeoutAfterPossibleWriteBroker(Broker):
    def __init__(
        self,
        clock: VirtualClock,
        account: AccountSnapshot,
        store: InMemoryExecutionStore,
    ) -> None:
        self.clock = clock
        self.account = account
        self.store = store
        self.place_calls = 0
        self.negative_confirmation = False
        descriptor = CapabilityDescriptor.from_schema(
            capability="place_option_order",
            tool_name="mock.place_option_order",
            schema_version="mock-v1",
            input_schema=PLACE_SCHEMA,
            side_effect_free=False,
        )
        self.capabilities = BrokerCapabilities(
            adapter_name="timeout-mock",
            adapter_version="v1",
            discovered_at=clock.now(),
            descriptors=(descriptor,),
            account_state=True,
            positions=True,
            orders=True,
            review_option_orders=True,
            place_option_orders=True,
            cancel_option_orders=True,
            reconcile=True,
            external_writes_enabled=True,
            execution_ready=True,
        )

    async def get_capabilities(self) -> BrokerCapabilities:
        return self.capabilities

    async def get_observed_broker_account_state(self) -> AccountSnapshot:
        return self.account

    async def get_effective_execution_account_state(self) -> AccountSnapshot:
        return self.account

    async def get_positions(self) -> tuple[Position, ...]:
        return ()

    async def get_orders(self) -> tuple[BrokerOrder, ...]:
        return ()

    async def review_option_order(self, proposal: TradeProposal) -> BrokerReview:
        return BrokerReview(
            created_at=self.clock.now(),
            environment=ExecutionEnvironment.LIVE,
            proposal_id=proposal.proposal_id,
            accepted=True,
            side_effect_free=True,
        )

    async def record_broker_command_intent(self, command: BrokerCommandIntent) -> None:
        del command

    async def place_option_order(
        self,
        command: BrokerCommandIntent,
        contract: OptionContract,
    ) -> BrokerOrder:
        del contract
        assert command.command_intent_id in self.store.command_intents
        self.place_calls += 1
        raise TimeoutError("response lost after transport accepted bytes")

    async def cancel_option_order(
        self,
        command: BrokerCommandIntent,
        order_id: object,
    ) -> BrokerOrder:
        del command, order_id
        raise NotImplementedError

    async def reconcile(self) -> ReconciliationReport:
        return ReconciliationReport(
            environment=ExecutionEnvironment.LIVE,
            reconciled_at=self.clock.now(),
            successful=True,
            observed_account=self.account,
            effective_account=self.account,
            position_count=0,
            open_order_count=0,
            details={
                "order_history_complete": self.negative_confirmation,
                "negative_match_idempotency_keys": [
                    item.idempotency_key for item in self.store.order_intents.values()
                ]
                if self.negative_confirmation
                else [],
            },
        )


def normal_safety(clock: VirtualClock) -> SafetyController:
    controller = SafetyController(clock, timedelta(0))
    evidence = SafetyEvidence(True, True, True, True, True, True, True, True)
    controller.observe(evidence)
    controller.observe(evidence)
    return controller


@pytest.mark.asyncio
async def test_possible_live_write_becomes_unknown_and_is_never_blindly_retried() -> None:
    clock = VirtualClock(NOW)
    contract = OptionContract(
        instrument_id="opt-live",
        symbol="TEST",
        option_type=OptionType.CALL,
        strike=Decimal("10"),
        expiration=date(2026, 9, 18),
    )
    quote = OptionQuote(
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
    proposal = TradeProposal(
        created_at=NOW,
        environment=ExecutionEnvironment.LIVE,
        namespace="live-test",
        packet_id="00000000-0000-0000-0000-000000000001",
        symbol="TEST",
        contract=contract,
        side=OrderSide.BUY_TO_OPEN,
        quantity=1,
        limit_price=Decimal("0.08"),
        quote_snapshot_id=quote.snapshot_id,
        quote_as_of=NOW,
        policy_version="live-conservative-v1",
        risk_config_version="risk-v1",
        thesis="fixture",
        invalidation_conditions=("invalid",),
    )
    account = AccountSnapshot(
        created_at=NOW,
        environment=ExecutionEnvironment.LIVE,
        account_kind=AccountKind.BROKER_OBSERVED,
        account_fingerprint="qualified-account",
        cash=Decimal("25"),
        buying_power=Decimal("25"),
        as_of=NOW,
        is_authenticated=True,
        state_known=True,
    )
    store = InMemoryExecutionStore()
    broker = TimeoutAfterPossibleWriteBroker(clock, account, store)
    service = ExecutionService(
        broker=broker,
        quotes=Quotes(quote),
        risk_engine=RiskEngine(load_config().risk, clock, live_capital_ceiling=Decimal("25")),
        store=store,
        clock=clock,
        safety=normal_safety(clock),
        environment=ExecutionEnvironment.LIVE,
        namespace="live-test",
        trading_mode=TradingMode.AUTO,
    )

    with pytest.raises(SubmissionUnknownError, match="do not retry"):
        await service.execute_entry(proposal)
    assert next(iter(store.orders.values())).state is OrderState.SUBMISSION_UNKNOWN

    with pytest.raises(SubmissionUnknownError, match="blind retry"):
        await service.execute_entry(proposal)
    assert broker.place_calls == 1

    reconciliation = await service.reconcile_submission(next(iter(store.order_intents)))
    assert not reconciliation.negative_match_proven
    assert not reconciliation.retry_permitted
    assert not store.negative_reconciliations

    broker.negative_confirmation = True
    reconciliation = await service.reconcile_submission(next(iter(store.order_intents)))
    assert reconciliation.negative_match_proven
    assert reconciliation.retry_permitted
    assert broker.place_calls == 1
