from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any
from uuid import UUID

import pytest
from pydantic import BaseModel

from app.clock.base import VirtualClock
from app.domain.enums import (
    AttentionLevel,
    Direction,
    JudgeDecision,
    OptionType,
    SelectorStatus,
)
from app.domain.models import (
    ContractSelection,
    OptionContract,
    OptionQuote,
    ProviderMetadata,
    sha256_json,
)
from app.reasoning.pipeline import PipelineStatus, ReasoningPipeline
from app.reasoning.policies import load_decision_policy_set
from app.reasoning.provider import (
    LocalModelProvider,
    ModelCallMetrics,
    ModelCallResult,
    ModelHealth,
    ReasoningRole,
)
from app.reasoning.schemas import (
    BearAnalysis,
    BullAnalysis,
    JudgeAnalysis,
    SituationAnalysis,
)
from app.strategy.candidates import CandidatePacketBuilder


class FakeProvider(LocalModelProvider):
    def __init__(self, outputs: dict[ReasoningRole, BaseModel]) -> None:
        self.outputs = outputs
        self.calls: list[tuple[ReasoningRole, str]] = []

    @property
    def model_name(self) -> str:
        return "fixture-model"

    async def generate(
        self,
        *,
        role: ReasoningRole,
        prompt: str,
        response_model: type[BaseModel],
        system_prompt: str = "",
        deep: bool = False,
    ) -> ModelCallResult[Any]:
        del system_prompt, deep
        self.calls.append((role, prompt))
        output = self.outputs[role]
        assert isinstance(output, response_model)
        return ModelCallResult(
            output=output,
            role=role,
            model_name=self.model_name,
            metrics=ModelCallMetrics(latency_ms=1),
            raw_response_hash=sha256_json(output.model_dump(mode="json")),
        )

    async def health(self) -> ModelHealth:
        return ModelHealth(
            healthy=True,
            model_name=self.model_name,
            model_present=True,
            detail="fixture",
        )


@pytest.mark.asyncio
async def test_pipeline_is_sequential_grounded_and_policy_gated() -> None:
    now = datetime(2026, 1, 5, 15, 0, tzinfo=UTC)
    clock = VirtualClock(now)
    packet = CandidatePacketBuilder(clock).build(
        run_id=UUID("11111111-1111-4111-8111-111111111111"),
        symbol="TEST",
        attention=AttentionLevel.CANDIDATE,
        surveillance_score=70,
        facts={"f1": "verified catalyst", "f2": "confirming volume"},
    )
    outputs: dict[ReasoningRole, BaseModel] = {
        ReasoningRole.SITUATION: SituationAnalysis(
            materiality="0.8",
            directional_bias=Direction.BULLISH,
            time_horizon="ten sessions",
            primary_driver="verified catalyst",
            supporting_facts=("f1", "f2"),
            uncertainties=("market regime",),
            thesis_invalidation_conditions=("event reversed",),
            research_needed=(),
            abstain_reason=None,
        ),
        ReasoningRole.BULL: BullAnalysis(
            thesis="catalyst reprices shares",
            supporting_fact_ids=("f1", "f2"),
            upside_drivers=("volume confirmation",),
            required_evidence=("follow through",),
            failure_conditions=("event reversed",),
            confidence="0.7",
        ),
        ReasoningRole.BEAR: BearAnalysis(
            downside_thesis="move may fade",
            supporting_fact_ids=("f1",),
            downside_drivers=("priced in",),
            bullish_option_failure_modes=("theta", "spread"),
            required_evidence=("failed follow through",),
            confidence="0.4",
        ),
        ReasoningRole.JUDGE: JudgeAnalysis(
            decision=JudgeDecision.PASS,
            directional_thesis=Direction.BULLISH,
            selected_candidate_rank=1,
            confidence="0.7",
            expected_time_window="ten sessions",
            catalyst_strength="0.8",
            contract_quality_critique="acceptable execution quality",
            thesis="verified event may drive repricing",
            invalidation_conditions=("event reversed",),
            recheck_conditions=("spread widens",),
            reasons_to_abstain=(),
            rationale="facts and adversarial review align",
            referenced_fact_ids=("f1", "f2"),
            evidence_complete=True,
        ),
    }
    provider = FakeProvider(outputs)
    quote = OptionQuote(
        contract=OptionContract(
            instrument_id="TEST-C-11",
            symbol="TEST",
            option_type=OptionType.CALL,
            strike=Decimal("11"),
            expiration=date(2026, 1, 23),
        ),
        bid=Decimal("0.15"),
        ask=Decimal("0.20"),
        volume=100,
        open_interest=500,
        metadata=ProviderMetadata(
            provider="fixture",
            capability_version="v1",
            observed_at=now,
            effective_at=now,
        ),
    )
    selection = ContractSelection(
        status=SelectorStatus.CONTRACT_FOUND,
        ranked_quotes=(quote,),
    )
    profiles = load_decision_policy_set()
    run = await ReasoningPipeline(provider, clock).run(
        packet,
        policy_name="DEMO_EXPLORATORY",
        policy_profile=profiles.profiles["DEMO_EXPLORATORY"],
        trade_quality_score=70,
        contract_selection=selection,
    )
    assert run.status is PipelineStatus.JUDGED
    assert run.policy_outcome is not None and run.policy_outcome.proceed
    assert [role for role, _ in provider.calls] == [
        ReasoningRole.SITUATION,
        ReasoningRole.BULL,
        ReasoningRole.BEAR,
        ReasoningRole.JUDGE,
    ]
    bull_packet = provider.calls[1][1].split("UNTRUSTED_PACKET_JSON:\n", 1)[1]
    bear_packet = provider.calls[2][1].split("UNTRUSTED_PACKET_JSON:\n", 1)[1]
    assert bull_packet == bear_packet
    assert len(run.analyses) == 4
