from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
from decimal import Decimal

import pytest

from app.broker.simulated import SimulatedBroker
from app.clock.base import VirtualClock
from app.config import load_config
from app.domain.enums import ExecutionEnvironment, TradingMode
from app.domain.models import (
    BrokerCommandIntent,
    BrokerReview,
    OptionQuote,
    ProviderMetadata,
    TradeProposal,
)
from app.execution import ExecutionDenied, ExecutionService, InMemoryExecutionStore
from app.risk import RiskEngine
from app.safety.runtime_state import SafetyController, SafetyEvidence


@dataclass
class _Controls:
    mode: TradingMode = TradingMode.AUTO
    killed: bool = False


class _Quote:
    def __init__(self, quote: OptionQuote) -> None:
        self.quote = quote

    async def get_option_quote(self, instrument_id: str) -> OptionQuote:
        assert instrument_id == self.quote.contract.instrument_id
        return self.quote


@pytest.mark.parametrize("stage", ["review", "command_journal"])
@pytest.mark.parametrize("change", ["mode", "kill_switch"])
@pytest.mark.asyncio
async def test_control_change_after_admission_prevents_broker_transmission(
    clock: VirtualClock,
    proposal: TradeProposal,
    monkeypatch: pytest.MonkeyPatch,
    stage: str,
    change: str,
) -> None:
    quote = OptionQuote(
        contract=proposal.contract,
        bid=Decimal("0.07"),
        ask=Decimal("0.08"),
        volume=100,
        open_interest=500,
        bid_size=10,
        ask_size=10,
        metadata=ProviderMetadata(
            provider="fixture",
            capability_version="v1",
            observed_at=clock.now(),
            effective_at=clock.now(),
        ),
    )
    proposal = proposal.model_copy(update={"quote_snapshot_id": quote.snapshot_id})
    broker = SimulatedBroker(clock=clock, namespace=proposal.namespace)
    await broker.consume_quote(quote)
    store = InMemoryExecutionStore()
    controls = _Controls()
    safety = SafetyController(clock, timedelta(0))
    evidence = SafetyEvidence(True, True, True, True, True, True, True, True)
    safety.observe(evidence)
    safety.observe(evidence)
    service = ExecutionService(
        broker=broker,
        quotes=_Quote(quote),
        risk_engine=RiskEngine(load_config().risk, clock),
        store=store,
        clock=clock,
        safety=safety,
        environment=ExecutionEnvironment.DEMO,
        namespace=proposal.namespace,
        trading_mode=lambda: controls.mode,
        kill_switch_active=lambda: controls.killed,
    )

    def change_control() -> None:
        if change == "mode":
            controls.mode = TradingMode.RESEARCH
        else:
            controls.killed = True

    original_review = broker.review_option_order
    original_save_command = store.save_command_intent

    async def review_then_change(trade: TradeProposal) -> BrokerReview:
        review = await original_review(trade)
        change_control()
        return review

    async def journal_then_change(command: BrokerCommandIntent) -> None:
        await original_save_command(command)
        change_control()

    if stage == "review":
        monkeypatch.setattr(broker, "review_option_order", review_then_change)
    else:
        monkeypatch.setattr(store, "save_command_intent", journal_then_change)

    with pytest.raises(ExecutionDenied):
        await service.execute_entry(proposal)

    assert broker.recorded_command_intents == ()
    assert await broker.get_orders() == ()
