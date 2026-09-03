from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from uuid import UUID

import pytest

from app.clock.base import VirtualClock
from app.domain.enums import (
    AttentionLevel,
    Direction,
    JudgeDecision,
    OptionType,
    SelectorStatus,
)
from app.domain.models import ContractSelection, OptionContract, OptionQuote, ProviderMetadata
from app.reasoning import (
    BearAnalysis,
    BoundedDeepResearchWorker,
    BullAnalysis,
    DeepResearchStatus,
    JudgeAnalysis,
    ReasoningPipeline,
    ReasoningRole,
    ResearchClaim,
    ResearchDocument,
    ResearchSynthesis,
    ScriptedReplayModelProvider,
    SituationAnalysis,
    SkepticAnalysis,
    load_decision_policy_set,
)
from app.reasoning.pipeline import PipelineStatus
from app.strategy.candidates import CandidatePacketBuilder


@dataclass(frozen=True, slots=True)
class OneDocumentSource:
    source_id: str
    document: ResearchDocument

    async def search(self, query: str, *, limit: int) -> tuple[ResearchDocument, ...]:
        assert query.startswith("TEST:")
        assert limit == 12
        return (self.document,)


@pytest.mark.asyncio
async def test_deep_attention_runs_bounded_research_and_allows_document_grounding() -> None:
    now = datetime(2026, 1, 5, 15, 0, tzinfo=UTC)
    clock = VirtualClock(now)
    packet = CandidatePacketBuilder(clock).build(
        run_id=UUID("11111111-1111-4111-8111-111111111111"),
        symbol="TEST",
        attention=AttentionLevel.DEEP_RESEARCH,
        surveillance_score=Decimal("90"),
        facts={"f1": "verified catalyst"},
    )
    research_document = ResearchDocument(
        document_id="sec-8k-1",
        source_id="sec",
        title="Issuer filing",
        url="https://www.sec.gov/Archives/example",
        effective_at=now - timedelta(minutes=2),
        observed_at=now - timedelta(minutes=1),
        excerpt="The issuer disclosed a material signed agreement.",
    )
    outputs = {
        ReasoningRole.SITUATION: SituationAnalysis(
            materiality="0.9",
            directional_bias=Direction.BULLISH,
            time_horizon="ten sessions",
            primary_driver="signed agreement",
            supporting_facts=("f1",),
            uncertainties=("terms",),
            thesis_invalidation_conditions=("agreement terminated",),
            research_needed=("verify filed agreement",),
            abstain_reason=None,
        ),
        ReasoningRole.BULL: BullAnalysis(
            thesis="agreement may reprice shares",
            supporting_fact_ids=("f1",),
            upside_drivers=("commercial validation",),
            required_evidence=("filed agreement",),
            failure_conditions=("termination",),
            confidence="0.8",
        ),
        ReasoningRole.BEAR: BearAnalysis(
            downside_thesis="economics may disappoint",
            supporting_fact_ids=("f1",),
            downside_drivers=("unknown terms",),
            bullish_option_failure_modes=("theta",),
            required_evidence=("economics",),
            confidence="0.4",
        ),
        ReasoningRole.SKEPTIC: SkepticAnalysis(
            hidden_assumptions=("economics are favorable",),
            stale_or_circular_evidence=(),
            catalyst_priced_in_arguments=("initial move may price it",),
            timing_challenges=("implementation timing unknown",),
            contract_economics_challenges=("theta",),
            thesis_right_but_option_loses=("move is too small",),
            decision_changing_data=("agreement economics",),
            referenced_fact_ids=("f1",),
            unresolved_primary_source_conflict=False,
        ),
        ReasoningRole.DEEP_RESEARCH: ResearchSynthesis(
            summary="The primary filing confirms a signed agreement.",
            claims=(
                ResearchClaim(
                    claim="A signed agreement was disclosed.",
                    document_ids=(research_document.document_id,),
                ),
            ),
            counterevidence=(),
            uncertainties=("economics remain undisclosed",),
            research_needed=(),
            abstain_reason=None,
        ),
        ReasoningRole.JUDGE: JudgeAnalysis(
            decision=JudgeDecision.PASS,
            directional_thesis=Direction.BULLISH,
            selected_candidate_rank=1,
            confidence="0.85",
            expected_time_window="ten sessions",
            catalyst_strength="0.9",
            contract_quality_critique="acceptable spread and liquidity",
            thesis="primary filing confirms the catalyst",
            invalidation_conditions=("agreement terminated",),
            recheck_conditions=("terms disclosed",),
            reasons_to_abstain=(),
            rationale="primary-source confirmation supports the setup",
            referenced_fact_ids=(research_document.document_id,),
            evidence_complete=True,
        ),
    }
    provider = ScriptedReplayModelProvider(outputs, script_version="deep-pipeline-v1")
    worker = BoundedDeepResearchWorker(
        clock,
        provider,
        (OneDocumentSource("sec", research_document),),
    )
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
    profiles = load_decision_policy_set()

    run = await ReasoningPipeline(
        provider,
        clock,
        deep_research_worker=worker,
    ).run(
        packet,
        policy_name="DEMO_EXPLORATORY",
        policy_profile=profiles.profiles["DEMO_EXPLORATORY"],
        trade_quality_score=Decimal("90"),
        contract_selection=ContractSelection(
            status=SelectorStatus.CONTRACT_FOUND,
            ranked_quotes=(quote,),
        ),
    )

    assert run.status is PipelineStatus.JUDGED
    assert run.deep_research is not None
    assert run.deep_research.status is DeepResearchStatus.COMPLETE
    assert run.deep_research.model_calls == 1
    assert run.grounding[-1].grounded
    assert research_document.document_id in run.grounding[-1].available_fact_ids
    assert provider.calls == [
        ReasoningRole.SITUATION,
        ReasoningRole.BULL,
        ReasoningRole.BEAR,
        ReasoningRole.SKEPTIC,
        ReasoningRole.DEEP_RESEARCH,
        ReasoningRole.JUDGE,
    ]
