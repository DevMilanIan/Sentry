from __future__ import annotations

from datetime import timedelta
from decimal import Decimal
from uuid import uuid4

import pytest

from app.broker.simulated import SimulatedBroker
from app.clock.base import VirtualClock
from app.config import load_config
from app.domain.enums import ExecutionEnvironment, OrderState, TradingMode
from app.domain.models import OptionQuote, ProviderMetadata, TradeProposal
from app.execution import ExecutionDenied, ExecutionService, InMemoryExecutionStore
from app.risk import RiskEngine
from app.safety.runtime_state import SafetyController, SafetyEvidence


class _Quotes:
    def __init__(self, quotes: tuple[OptionQuote, ...]) -> None:
        self._quotes = {quote.contract.instrument_id: quote for quote in quotes}

    async def get_option_quote(self, instrument_id: str) -> OptionQuote:
        return self._quotes[instrument_id]


@pytest.mark.asyncio
async def test_pending_entry_is_counted_before_another_entry_can_pass_risk(
    clock: VirtualClock, proposal: TradeProposal
) -> None:
    first_quote = OptionQuote(
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
    second_quote = first_quote.model_copy(
        update={
            "snapshot_id": uuid4(),
            "contract": proposal.contract.model_copy(
                update={"instrument_id": "opt-second", "symbol": "NEXT"}
            ),
        }
    )
    first = proposal.model_copy(update={"quote_snapshot_id": first_quote.snapshot_id})
    second = proposal.model_copy(
        update={
            "proposal_id": uuid4(),
            "symbol": "NEXT",
            "contract": second_quote.contract,
            "quote_snapshot_id": second_quote.snapshot_id,
        }
    )
    broker = SimulatedBroker(clock=clock, namespace=proposal.namespace)
    await broker.consume_quote(first_quote)
    await broker.consume_quote(second_quote)
    safety = SafetyController(clock, timedelta(0))
    evidence = SafetyEvidence(True, True, True, True, True, True, True, True)
    safety.observe(evidence)
    safety.observe(evidence)
    service = ExecutionService(
        broker=broker,
        quotes=_Quotes((first_quote, second_quote)),
        risk_engine=RiskEngine(load_config().risk, clock),
        store=InMemoryExecutionStore(),
        clock=clock,
        safety=safety,
        environment=ExecutionEnvironment.DEMO,
        namespace=proposal.namespace,
        trading_mode=TradingMode.AUTO,
    )

    accepted = await service.execute_entry(first)
    assert accepted.broker_order.state is OrderState.OPEN

    with pytest.raises(ExecutionDenied):
        await service.execute_entry(second)

    account = await broker.get_effective_execution_account_state()
    assert account.open_option_risk == Decimal("8")
    assert account.open_positions == 1
    assert account.new_entries_today == 1
