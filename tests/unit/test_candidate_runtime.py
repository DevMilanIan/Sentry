from __future__ import annotations

import asyncio
from datetime import timedelta
from decimal import Decimal
from typing import Any
from uuid import uuid4

import pytest
from pydantic import BaseModel

from app.catalysts.models import SourceDocument
from app.clock.base import VirtualClock
from app.config import LoadedConfig, load_config
from app.db.repository import InMemoryAuditRepository
from app.domain.enums import Direction, JudgeDecision
from app.domain.models import SentinelEvent, sha256_json
from app.exceptions import SafetyCriticalError
from app.market.fixtures import bundled_fixture_path
from app.market.models import ReplayFixture
from app.market.replay import OfflineReplayMarketDataProvider
from app.reasoning.policies import load_decision_policy_set
from app.reasoning.provider import ModelCallResult, ReasoningRole
from app.reasoning.schemas import BearAnalysis, BullAnalysis, JudgeAnalysis, SituationAnalysis
from app.reasoning.scripted import ScriptedReplayModelProvider
from app.strategy.candidates import CandidateFact
from app.strategy.runtime import (
    CandidateFeatureSet,
    CandidateInputs,
    CandidateResearchWorker,
    CandidateWorkerLimits,
    FeatureProvider,
)


@pytest.fixture
def loaded(monkeypatch: pytest.MonkeyPatch) -> LoadedConfig:
    for name in ("SENTRY_EXECUTION_ENVIRONMENT", "SENTRY_DEMO_BACKEND", "SENTRY_TRADING_MODE"):
        monkeypatch.delenv(name, raising=False)
    return load_config()


@pytest.fixture
def market() -> OfflineReplayMarketDataProvider:
    fixture = ReplayFixture.model_validate_json(
        bundled_fixture_path("offline_e2e_session.json").read_text(encoding="utf-8")
    )
    first = fixture.records[0]
    next_time = fixture.records[1].observed_at
    next_quote = first.model_copy(
        update={
            "sequence": 1,
            "observed_at": next_time,
            "effective_at": next_time,
            "payload": {
                **first.payload,
                "snapshot_id": str(uuid4()),
                "bid": "10.09",
                "ask": "10.11",
                "last": "10.10",
                "metadata": {
                    **first.payload["metadata"],
                    "observed_at": next_time.isoformat(),
                    "effective_at": next_time.isoformat(),
                },
            },
        }
    )
    records = (
        first,
        next_quote,
        *(
            record.model_copy(update={"sequence": record.sequence + 1})
            for record in fixture.records[1:]
        ),
    )
    return OfflineReplayMarketDataProvider(
        fixture.model_copy(update={"records": records}), VirtualClock(first.observed_at)
    )


def outputs(fact_id: str = "verified_metric") -> dict[ReasoningRole, BaseModel]:
    return {
        ReasoningRole.SITUATION: SituationAnalysis(
            materiality="0.8",
            directional_bias=Direction.BULLISH,
            time_horizon="two weeks",
            primary_driver="supplied measured test evidence",
            supporting_facts=(fact_id,),
            uncertainties=("future price",),
            thesis_invalidation_conditions=("reversal",),
            research_needed=(),
            abstain_reason=None,
        ),
        ReasoningRole.BULL: BullAnalysis(
            thesis="measured evidence supports a hypothesis",
            supporting_fact_ids=(fact_id,),
            upside_drivers=("continued measured change",),
            required_evidence=("follow-through",),
            failure_conditions=("reversal",),
            confidence="0.8",
        ),
        ReasoningRole.BEAR: BearAnalysis(
            downside_thesis="change can reverse",
            supporting_fact_ids=(fact_id,),
            downside_drivers=("reversal",),
            bullish_option_failure_modes=("theta",),
            required_evidence=("follow-through",),
            confidence="0.4",
        ),
        ReasoningRole.JUDGE: JudgeAnalysis(
            decision=JudgeDecision.PASS,
            directional_thesis=Direction.BULLISH,
            selected_candidate_rank=1,
            confidence="0.8",
            expected_time_window="two weeks",
            catalyst_strength="0.5",
            contract_quality_critique="finite executable test market",
            thesis="measured test hypothesis",
            invalidation_conditions=("reversal",),
            recheck_conditions=("spread widens",),
            reasons_to_abstain=(),
            rationale="provided test evidence only",
            referenced_fact_ids=(fact_id,),
            evidence_complete=True,
        ),
    }


def make_worker(
    loaded: LoadedConfig,
    market: OfflineReplayMarketDataProvider,
    repository: InMemoryAuditRepository,
    provider: ScriptedReplayModelProvider,
    **kwargs: Any,
) -> CandidateResearchWorker:
    return CandidateResearchWorker(
        loaded,
        market._clock,
        repository,
        provider,
        policy_profile=load_decision_policy_set().profiles["DEMO_EXPLORATORY"],
        **kwargs,
    )


def event(market: OfflineReplayMarketDataProvider, **changes: Any) -> SentinelEvent:
    now = market._clock.now()
    return SentinelEvent(
        event_id=changes.pop("event_id", uuid4()),
        created_at=now,
        effective_at=now,
        event_type="REPLAY_MARKET_OBSERVATION",
        source=market.identity,
        tickers=("ACME",),
        severity=0,
        deduplication_key=str(uuid4()),
        **changes,
    )


def high_features(loaded: LoadedConfig) -> FeatureProvider:
    async def build(inputs: CandidateInputs) -> CandidateFeatureSet:
        fact = CandidateFact(
            fact_id="verified_metric",
            value={"test_measurements": "explicit injected values"},
            source_id="deterministic-test-extractor",
            effective_at=inputs.event.effective_at,
            observed_at=inputs.event.created_at,
        )
        surveillance = {key: Decimal("100") for key in loaded.strategy.scoring.surveillance}
        quality = {
            key: Decimal("100")
            for key in loaded.strategy.scoring.trade_quality
            if key != "contract_execution"
        }
        refs = {
            f"{group}:{key}": (fact.fact_id,)
            for group, values in (("surveillance", surveillance), ("trade_quality", quality))
            for key in values
        }
        return CandidateFeatureSet(
            facts=(fact,), surveillance=surveillance, trade_quality=quality, component_fact_ids=refs
        )

    return build


async def advance(market: OfflineReplayMarketDataProvider) -> None:
    assert isinstance(market._clock, VirtualClock)
    await market._clock.advance(timedelta(seconds=1))


async def test_first_quote_waits_and_ignores_event_payload_scores(
    loaded: LoadedConfig,
    market: OfflineReplayMarketDataProvider,
) -> None:
    repository = InMemoryAuditRepository(loaded.bind_runtime())
    provider = ScriptedReplayModelProvider(outputs())
    worker = make_worker(
        loaded, market, repository, provider, feature_provider=high_features(loaded)
    )
    incoming = event(market, payload={"surveillance_score": 100, "verified_catalyst": True})
    assert await worker.on_event(incoming, market) is None
    assert provider.calls == []
    row = await repository.find_payload("candidate_runs", "source_event_id", str(incoming.event_id))
    assert row is not None and row["payload"]["status"] == "WAIT"
    assert row["payload"]["reason"] == "no_measurable_change_or_verified_source"


async def test_missing_components_are_zero_and_configured_weights_are_used(
    loaded: LoadedConfig,
    market: OfflineReplayMarketDataProvider,
) -> None:
    repository = InMemoryAuditRepository(loaded.bind_runtime())
    provider = ScriptedReplayModelProvider(outputs())
    worker = make_worker(loaded, market, repository, provider)
    await worker.on_event(event(market), market)
    await advance(market)
    assert await worker.on_event(event(market), market) is None
    rows = await repository.list("candidate_features")
    score = next(row["payload"]["surveillance"] for row in rows if "surveillance" in row["payload"])
    assert "catalyst_priority" in score["missing_components"]
    assert "federal_exposure" in score["missing_components"]
    assert Decimal(score["final_score"]) < loaded.strategy.attention_thresholds.l2_candidate
    for component in score["components"]:
        assert (
            Decimal(component["weight"]) == loaded.strategy.scoring.surveillance[component["name"]]
        )
        if not component["supplied"]:
            assert Decimal(component["raw_value"]) == 0
    assert provider.calls == []


async def test_grounded_injected_features_produce_stable_audited_proposal_and_restart_dedup(
    loaded: LoadedConfig,
    market: OfflineReplayMarketDataProvider,
) -> None:
    repository = InMemoryAuditRepository(loaded.bind_runtime())
    provider = ScriptedReplayModelProvider(outputs())
    worker = make_worker(
        loaded, market, repository, provider, feature_provider=high_features(loaded)
    )
    await worker.on_event(event(market), market)
    await advance(market)
    incoming = event(market)
    proposal = await worker.on_event(incoming, market)
    assert proposal is not None and proposal.quantity == 1
    assert proposal.namespace == loaded.bind_runtime().idempotency_namespace
    assert proposal.quote_as_of <= proposal.created_at
    assert provider.calls == [
        ReasoningRole.SITUATION,
        ReasoningRole.BULL,
        ReasoningRole.BEAR,
        ReasoningRole.JUDGE,
    ]
    assert await repository.list("trade_proposals") == []
    result = await repository.find_payload(
        "candidate_runs", "source_event_id", str(incoming.event_id)
    )
    assert result is not None and result["payload"]["proposal"] == proposal.model_dump(mode="json")
    calls = await repository.list("model_calls")
    assert len([row for row in calls if row["payload"]["status"] == "SUCCEEDED"]) == 4
    assert all(row["payload"]["data_mode"] == "REPLAY" for row in calls)
    restarted_provider = ScriptedReplayModelProvider(outputs())
    restarted = make_worker(
        loaded, market, repository, restarted_provider, feature_provider=high_features(loaded)
    )
    assert await restarted.on_event(incoming, market) == proposal
    assert restarted_provider.calls == []
    assert await repository.list("trade_proposals") == []


async def test_reused_event_id_with_changed_content_fails_closed(
    loaded: LoadedConfig,
    market: OfflineReplayMarketDataProvider,
) -> None:
    repository = InMemoryAuditRepository(loaded.bind_runtime())
    worker = make_worker(loaded, market, repository, ScriptedReplayModelProvider(outputs()))
    incoming = event(market)
    await worker.on_event(incoming, market)
    with pytest.raises(SafetyCriticalError, match="conflicting content"):
        await worker.on_event(incoming.model_copy(update={"payload": {"changed": True}}), market)


async def test_timeout_persists_wait_and_model_attempt_then_deduplicates(
    loaded: LoadedConfig,
    market: OfflineReplayMarketDataProvider,
) -> None:
    class HangingProvider(ScriptedReplayModelProvider):
        async def generate[T: BaseModel](
            self,
            *,
            role: ReasoningRole,
            prompt: str,
            response_model: type[T],
            system_prompt: str = "",
            deep: bool = False,
        ) -> ModelCallResult[T]:
            await asyncio.Event().wait()
            raise AssertionError("unreachable")

    repository = InMemoryAuditRepository(loaded.bind_runtime())
    provider = HangingProvider(outputs())
    worker = make_worker(
        loaded,
        market,
        repository,
        provider,
        feature_provider=high_features(loaded),
        limits=CandidateWorkerLimits(maximum_seconds=0.02),
    )
    await worker.on_event(event(market), market)
    await advance(market)
    incoming = event(market)
    assert await worker.on_event(incoming, market) is None
    row = await repository.find_payload("candidate_runs", "source_event_id", str(incoming.event_id))
    assert row is not None and row["payload"]["reason"] == "research_timeout"
    assert len(await repository.list("model_calls")) == 1
    assert await worker.on_event(incoming, market) is None
    assert len(await repository.list("model_calls")) == 1


async def test_concurrent_duplicate_calls_do_not_generate_models_in_parallel(
    loaded: LoadedConfig,
    market: OfflineReplayMarketDataProvider,
) -> None:
    class TrackingProvider(ScriptedReplayModelProvider):
        active = 0
        maximum_active = 0

        async def generate[T: BaseModel](
            self,
            *,
            role: ReasoningRole,
            prompt: str,
            response_model: type[T],
            system_prompt: str = "",
            deep: bool = False,
        ) -> ModelCallResult[T]:
            self.active += 1
            self.maximum_active = max(self.maximum_active, self.active)
            try:
                await asyncio.sleep(0)
                return await super().generate(
                    role=role,
                    prompt=prompt,
                    response_model=response_model,
                    system_prompt=system_prompt,
                    deep=deep,
                )
            finally:
                self.active -= 1

    repository = InMemoryAuditRepository(loaded.bind_runtime())
    provider = TrackingProvider(outputs())
    worker = make_worker(
        loaded, market, repository, provider, feature_provider=high_features(loaded)
    )
    await worker.on_event(event(market), market)
    await advance(market)
    incoming = event(market)
    first, second = await asyncio.gather(
        worker.on_event(incoming, market), worker.on_event(incoming, market)
    )
    assert first is not None and first == second
    assert provider.maximum_active == 1 and len(provider.calls) == 4


async def test_future_feature_and_ungrounded_model_output_cannot_propose(
    loaded: LoadedConfig,
    market: OfflineReplayMarketDataProvider,
) -> None:
    async def future(inputs: CandidateInputs) -> CandidateFeatureSet:
        features = await high_features(loaded)(inputs)
        fact = features.facts[0].model_copy(
            update={"observed_at": inputs.event.created_at + timedelta(days=1)}
        )
        return features.model_copy(update={"facts": (fact,)})

    repository = InMemoryAuditRepository(loaded.bind_runtime())
    provider = ScriptedReplayModelProvider(outputs("invented_fact"))
    worker = make_worker(loaded, market, repository, provider, feature_provider=future)
    await worker.on_event(event(market), market)
    await advance(market)
    assert await worker.on_event(event(market), market) is None
    assert provider.calls == []
    # A persisted verified source allows a fresh event without a new quote baseline.
    source = await add_source(repository, market)
    grounded_features = make_worker(
        loaded, market, repository, provider, feature_provider=high_features(loaded)
    )
    assert (
        await grounded_features.on_event(
            event(market, raw_reference_ids=(str(source.document_id),)), market
        )
        is None
    )
    assert provider.calls == [ReasoningRole.SITUATION]
    assert await repository.list("trade_proposals") == []


async def add_source(
    repository: InMemoryAuditRepository, market: OfflineReplayMarketDataProvider
) -> SourceDocument:
    now = market._clock.now()
    source = SourceDocument(
        source_id="sec",
        canonical_url="https://www.sec.gov/news/test-source",
        title="Test issuer document",
        normalized_text="Test-only source evidence for ACME.",
        publication_time=now,
        fetched_at=now,
        created_at=now,
        tickers=("ACME",),
    )
    await repository.append("source_documents", source)
    return source


async def test_persisted_configured_source_can_trigger_without_price_baseline(
    loaded: LoadedConfig,
    market: OfflineReplayMarketDataProvider,
) -> None:
    await advance(market)
    repository = InMemoryAuditRepository(loaded.bind_runtime())
    source = await add_source(repository, market)
    worker = make_worker(
        loaded,
        market,
        repository,
        ScriptedReplayModelProvider(outputs()),
        feature_provider=high_features(loaded),
    )
    assert (
        await worker.on_event(event(market, raw_reference_ids=(str(source.document_id),)), market)
        is not None
    )
    packets = await repository.list("candidate_packets")
    facts = packets[0]["payload"]["facts"]
    assert f"source:{source.document_id}" in facts
    assert "measured_price_change" not in facts


async def test_interrupted_persisted_attempt_is_not_regenerated(
    loaded: LoadedConfig,
    market: OfflineReplayMarketDataProvider,
) -> None:
    repository = InMemoryAuditRepository(loaded.bind_runtime())
    incoming = event(market)
    await repository.append(
        "candidate_runs",
        {
            "event_key": f"{repository.binding.idempotency_namespace}:{incoming.event_id}",
            "event_hash": sha256_json(incoming.model_dump(mode="json")),
            "status": "STARTED",
        },
    )
    provider = ScriptedReplayModelProvider(outputs())
    worker = make_worker(loaded, market, repository, provider)
    assert await worker.on_event(incoming, market) is None
    assert provider.calls == []
    row = await repository.find_payload("candidate_runs", "source_event_id", str(incoming.event_id))
    assert row is not None and row["payload"]["reason"] == "interrupted_prior_attempt"


async def test_audit_failure_prevents_model_call(
    loaded: LoadedConfig,
    market: OfflineReplayMarketDataProvider,
) -> None:
    repository = InMemoryAuditRepository(loaded.bind_runtime())
    repository.writable = False
    provider = ScriptedReplayModelProvider(outputs())
    worker = make_worker(loaded, market, repository, provider)
    with pytest.raises(SafetyCriticalError, match="not writable"):
        await worker.on_event(event(market), market)
    assert provider.calls == []
