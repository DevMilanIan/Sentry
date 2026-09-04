from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Mapping
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any, Protocol
from urllib.parse import urlsplit
from uuid import NAMESPACE_URL, UUID, uuid5

from pydantic import BaseModel, Field

from app.catalysts.entity_mapping import (
    EntityMappingStatus,
    ExplicitIssuerMapper,
    SourceEntityMapping,
)
from app.catalysts.models import SourceDocument
from app.clock.base import Clock
from app.config import LoadedConfig, RuntimeBinding
from app.domain.enums import (
    AttentionLevel,
    DemoBackend,
    Direction,
    ExecutionEnvironment,
    OptionType,
    OrderSide,
)
from app.domain.models import (
    DomainModel,
    EquityQuote,
    OptionQuote,
    SentinelEvent,
    TradeProposal,
    sha256_json,
)
from app.exceptions import DataInvalidError, SafetyCriticalError
from app.federal.service import FederalRegistryService
from app.market.base import MarketDataProvider
from app.market.models import PriceBar
from app.options.selector import ContractSelector, ContractSelectorConfig
from app.reasoning.pipeline import ReasoningPipeline
from app.reasoning.policies import DecisionPolicyProfile
from app.reasoning.provider import LocalModelProvider, ModelCallResult, ModelHealth, ReasoningRole
from app.strategy.attention import AttentionMapper, AttentionThresholds
from app.strategy.candidates import CandidateFact, CandidatePacketBuilder
from app.strategy.scoring import SurveillanceScorer, TradeQualityScorer


class CandidateAuditRepository(Protocol):
    binding: RuntimeBinding

    async def append(self, table: str, value: BaseModel | Mapping[str, Any]) -> UUID: ...

    async def find_payload(self, table: str, key: str, value: Any) -> dict[str, Any] | None: ...


class CandidateWorkerLimits(DomainModel):
    maximum_seconds: float = Field(default=180, gt=0)
    maximum_facts: int = Field(default=32, ge=2, le=128)
    maximum_feature_bytes: int = Field(default=32_000, ge=1_000, le=1_000_000)
    maximum_packet_tokens: int = Field(default=4_000, ge=256)
    maximum_option_quotes: int = Field(default=200, ge=1, le=10_000)
    maximum_bars: int = Field(default=1_000, ge=2, le=10_000)
    maximum_source_documents: int = Field(default=4, ge=1, le=20)
    maximum_source_characters: int = Field(default=2_000, ge=100, le=10_000)
    maximum_federal_relationships: int = Field(default=16, ge=1, le=64)
    bar_timeframe: str = "5m"
    bar_lookback_seconds: int = Field(default=86_400, gt=0)
    minimum_momentum_percent: Decimal = Field(default=Decimal("0.5"), gt=0)
    full_scale_momentum_percent: Decimal = Field(default=Decimal("5"), gt=0)
    full_scale_observed_dollar_volume: Decimal = Field(default=Decimal("10000000"), gt=0)


class CandidateInputs(DomainModel):
    event: SentinelEvent
    symbol: str
    current: EquityQuote
    previous: EquityQuote | None
    bars: tuple[PriceBar, ...]
    verified_source_facts: tuple[CandidateFact, ...]
    official_source_priority: Decimal | None = Field(default=None, ge=0, le=100)
    federal_registry_facts: tuple[CandidateFact, ...] = ()
    federal_exposure_score: Decimal | None = Field(default=None, ge=0, le=100)


class CandidateFeatureSet(DomainModel):
    """Trusted deterministic extractor output, never values read from event text.

    Every supplied score needs supporting fact IDs under ``surveillance:NAME`` or
    ``trade_quality:NAME``. Missing components stay absent and score zero. A custom
    extractor cannot bypass the independent measured-change/verified-source gate.
    """

    facts: tuple[CandidateFact, ...] = ()
    surveillance: dict[str, Decimal] = Field(default_factory=dict)
    trade_quality: dict[str, Decimal] = Field(default_factory=dict)
    component_fact_ids: dict[str, tuple[str, ...]] = Field(default_factory=dict)


type FeatureProvider = Callable[[CandidateInputs], Awaitable[CandidateFeatureSet]]
type AuditWriter = Callable[[str, BaseModel | Mapping[str, Any]], Awaitable[None]]


class _AuditedProvider(LocalModelProvider):
    def __init__(self, provider: LocalModelProvider, write: AuditWriter) -> None:
        self.provider, self.write = provider, write

    @property
    def model_name(self) -> str:
        return self.provider.model_name

    async def health(self) -> ModelHealth:
        return await self.provider.health()

    async def generate[T: BaseModel](
        self,
        *,
        role: ReasoningRole,
        prompt: str,
        response_model: type[T],
        system_prompt: str = "",
        deep: bool = False,
    ) -> ModelCallResult[T]:
        await self.write(
            "model_calls",
            {
                "role": role.value,
                "model_name": self.model_name,
                "status": "STARTED",
                "prompt_hash": sha256_json({"prompt": prompt, "system": system_prompt}),
            },
        )
        try:
            result = await self.provider.generate(
                role=role,
                prompt=prompt,
                response_model=response_model,
                system_prompt=system_prompt,
                deep=deep,
            )
        except Exception as exc:
            await self.write(
                "model_calls",
                {"role": role.value, "status": "FAILED", "error_type": type(exc).__name__},
            )
            raise
        await self.write("model_calls", {**result.model_dump(mode="json"), "status": "SUCCEEDED"})
        return result


class CandidateResearchWorker:
    """One serialized, bounded research attempt per persisted event identity.

    This worker proposes; it has no broker or execution authority. Events must
    identify exactly one ticker. A first quote is only a baseline. Later measured
    change or a causally available, persisted configured official-source document
    can trigger surveillance. Event payload scores/catalyst claims are not trusted.
    Scores missing evidence remain zero; no model confidence is converted into a
    deterministic feature score. Default trade-quality evidence is deliberately
    sparse and may correctly produce no proposal.

    STARTED is durable before provider calls. An interrupted attempt is not
    regenerated after restart; a new event is required. Completed duplicate calls
    return the same persisted proposal (if any), for downstream idempotent handling.
    The application must keep its environment single-instance lock: this local
    lock serializes model work but is not a distributed database claim.
    """

    def __init__(
        self,
        loaded: LoadedConfig,
        clock: Clock,
        repository: CandidateAuditRepository,
        provider: LocalModelProvider,
        *,
        policy_profile: DecisionPolicyProfile,
        policy_name: str = "DEMO_EXPLORATORY",
        selector: ContractSelector | None = None,
        feature_provider: FeatureProvider | None = None,
        federal_registry: FederalRegistryService | None = None,
        limits: CandidateWorkerLimits | None = None,
        run_id: UUID | None = None,
    ) -> None:
        self.loaded, self.clock, self.repository, self.provider = (
            loaded,
            clock,
            repository,
            provider,
        )
        self.binding = loaded.bind_runtime()
        if repository.binding != self.binding:
            raise SafetyCriticalError("candidate worker repository binding mismatch")
        if (
            self.binding.environment is ExecutionEnvironment.LIVE
            and policy_name != loaded.live.decision_policy
        ):
            raise SafetyCriticalError("LIVE candidate worker requires the configured LIVE policy")
        self.policy_profile, self.policy_name = policy_profile, policy_name
        self.limits = limits or CandidateWorkerLimits()
        self.feature_provider = feature_provider
        self.federal_registry = federal_registry
        if federal_registry is not None and federal_registry.repository.binding != self.binding:
            raise SafetyCriticalError("candidate worker federal registry binding mismatch")
        self.run_id = run_id
        self._lock = asyncio.Lock()
        strategy = loaded.strategy
        options = strategy.options
        self.selector = selector or ContractSelector(
            ContractSelectorConfig.from_risk_config(
                loaded.risk,
                minimum_dte=options.minimum_dte,
                maximum_dte=options.maximum_dte,
                maximum_absolute_moneyness_percent=options.maximum_absolute_moneyness_percent,
                maximum_candidates=options.maximum_candidates_for_judge,
                version=f"{strategy.version}:selector",
            ),
            clock,
        )
        self.surveillance = SurveillanceScorer(
            version=f"{strategy.version}:surveillance", weights=strategy.scoring.surveillance
        )
        self.trade_quality = TradeQualityScorer(
            version=f"{strategy.version}:trade-quality", weights=strategy.scoring.trade_quality
        )
        thresholds = strategy.attention_thresholds
        self.attention = AttentionMapper(
            AttentionThresholds(
                watch=thresholds.l1_watch,
                candidate=thresholds.l2_candidate,
                trade_worthy=thresholds.l3_trade_worthy,
                deep_research=thresholds.l4_deep_research,
            )
        )

    async def on_event(
        self, event: SentinelEvent, market: MarketDataProvider
    ) -> TradeProposal | None:
        async with self._lock:
            event_key = f"{self.binding.idempotency_namespace}:{event.event_id}"
            event_hash = sha256_json(event.model_dump(mode="json"))
            existing = await self.repository.find_payload("candidate_runs", "event_key", event_key)
            if existing is not None:
                payload = existing["payload"]
                if payload.get("event_hash") != event_hash:
                    raise SafetyCriticalError("candidate event identity has conflicting content")
                if payload.get("status") == "STARTED":
                    await self._outcome(event, market, "WAIT", "interrupted_prior_attempt")
                saved_proposal = payload.get("proposal")
                return (
                    TradeProposal.model_validate(saved_proposal)
                    if saved_proposal is not None
                    else None
                )
            await self._outcome(event, market, "STARTED", "research_attempt_started")
            try:
                async with asyncio.timeout(self.limits.maximum_seconds):
                    proposal, reason = await self._process(event, market)
            except TimeoutError:
                proposal, reason = None, "research_timeout"
            except Exception as exc:
                proposal, reason = None, f"research_failed:{type(exc).__name__}"
            await self._outcome(
                event, market, "PROPOSED" if proposal is not None else "WAIT", reason, proposal
            )
            return proposal

    async def _process(
        self, event: SentinelEvent, market: MarketDataProvider
    ) -> tuple[TradeProposal | None, str]:
        now = self.clock.now()
        self._available(event.created_at, now)
        self._available(event.effective_at, now)
        symbols = tuple(sorted({symbol.strip().upper() for symbol in event.tickers}))
        if len(symbols) != 1 or not symbols[0] or len(symbols[0]) > 12:
            return None, "one_unambiguous_ticker_required"
        symbol = symbols[0]
        cutoff = event.created_at
        current = await market.get_equity_quote(symbol, as_of=cutoff)
        self._available(current.metadata.observed_at, cutoff)
        self._available(current.metadata.effective_at, cutoff)
        if current.symbol.upper() != symbol or current.last <= 0:
            return None, "invalid_underlying_quote"
        if not self._fresh(current.metadata.effective_at, now):
            return None, "stale_underlying_quote"
        baseline_key = f"{self.binding.idempotency_namespace}:{symbol}"
        saved = await self.repository.find_payload(
            "candidate_features", "baseline_key", baseline_key
        )
        previous = EquityQuote.model_validate(saved["payload"]["quote"]) if saved else None
        if previous is not None:
            self._available(previous.metadata.observed_at, cutoff)
            self._available(previous.metadata.effective_at, cutoff)
        if previous is None or current.metadata.effective_at > previous.metadata.effective_at:
            await self._write(
                event,
                market,
                "candidate_features",
                {"baseline_key": baseline_key, "quote": current.model_dump(mode="json")},
            )
        bars = tuple(
            await market.get_bars(
                symbol,
                start=cutoff - timedelta(seconds=self.limits.bar_lookback_seconds),
                end=cutoff,
                timeframe=self.limits.bar_timeframe,
                as_of=cutoff,
            )
        )
        if len(bars) > self.limits.maximum_bars:
            return None, "bar_budget_exceeded"
        for bar in bars:
            self._available(bar.metadata.observed_at, cutoff)
            self._available(bar.ends_at, cutoff)
            if bar.symbol.upper() != symbol or bar.timeframe != self.limits.bar_timeframe:
                raise DataInvalidError("market returned an unrelated bar")
        source_facts, source_priority = await self._source_facts(event, symbol, cutoff)
        federal_facts, federal_score = await self._federal_facts(symbol, cutoff)
        inputs = CandidateInputs(
            event=event,
            symbol=symbol,
            current=current,
            previous=previous,
            bars=tuple(sorted(bars, key=lambda bar: bar.ends_at)[-2:]),
            verified_source_facts=source_facts,
            official_source_priority=source_priority,
            federal_registry_facts=federal_facts,
            federal_exposure_score=federal_score,
        )
        features, changed = self._measured_features(inputs)
        if not changed and not source_facts:
            return None, "no_measurable_change_or_verified_source"
        if self.feature_provider is not None:
            additional = await self.feature_provider(inputs)
            if "federal_exposure" in additional.surveillance:
                raise DataInvalidError(
                    "federal exposure is reserved for the verified reference registry"
                )
            features = CandidateFeatureSet(
                facts=(*features.facts, *additional.facts),
                surveillance={**features.surveillance, **additional.surveillance},
                trade_quality={**features.trade_quality, **additional.trade_quality},
                component_fact_ids={**features.component_fact_ids, **additional.component_fact_ids},
            )
        self._validate_features(features, cutoff)
        surveillance = self.surveillance.score(features.surveillance)
        attention = self.attention.map(surveillance.final_score)
        await self._write(
            event,
            market,
            "candidate_features",
            {
                "symbol": symbol,
                "feature_extractor_version": "candidate-runtime-v1",
                "feature_limits": self.limits.model_dump(mode="json"),
                "features": features.model_dump(mode="json"),
                "surveillance": surveillance.model_dump(mode="json"),
            },
        )
        if attention < AttentionLevel.CANDIDATE:
            return None, "surveillance_below_candidate_threshold"
        chain = tuple(await market.get_option_chain(symbol, as_of=cutoff))
        if len(chain) > self.limits.maximum_option_quotes:
            return None, "option_quote_budget_exceeded"
        fresh_chain: list[OptionQuote] = []
        for quote in chain:
            self._available(quote.metadata.observed_at, cutoff)
            self._available(quote.metadata.effective_at, cutoff)
            if quote.contract.symbol.upper() != symbol:
                raise DataInvalidError("market returned an unrelated option")
            if self._fresh(quote.metadata.effective_at, now):
                fresh_chain.append(quote)
        if not fresh_chain:
            return None, "no_fresh_option_quotes"
        execution_scores: list[Decimal] = []
        for direction in (Direction.BULLISH, Direction.BEARISH):
            detail = self.selector.evaluate(direction, current.last, fresh_chain)
            for evaluation in detail.evaluations:
                if evaluation.quote.contract.option_type is (
                    OptionType.CALL if direction is Direction.BULLISH else OptionType.PUT
                ):
                    await self._write(event, market, "contract_candidates", evaluation)
                if evaluation.eligible:
                    quote = evaluation.quote
                    execution_scores.append(
                        evaluation.quality_score
                        if quote.open_interest is not None
                        and quote.volume is not None
                        and quote.delta is not None
                        else Decimal("0")
                    )
        if not execution_scores:
            return None, "no_eligible_contract"
        # The direction/rank is not yet known, so use the worst eligible execution
        # quality. Missing liquidity/Greek inputs cannot inherit selector defaults.
        execution_score = min(execution_scores)
        quality_values = dict(features.trade_quality)
        if "contract_execution" in self.trade_quality.weights.weights:
            quality_values["contract_execution"] = execution_score
        quality = self.trade_quality.score(quality_values)
        await self._write(
            event, market, "candidate_features", {"trade_quality": quality.model_dump(mode="json")}
        )
        packet = CandidatePacketBuilder(
            self.clock, maximum_estimated_tokens=self.limits.maximum_packet_tokens
        ).build(
            run_id=self.run_id or uuid5(NAMESPACE_URL, f"candidate:{event.event_id}"),
            symbol=symbol,
            attention=attention,
            surveillance_score=surveillance.final_score,
            facts=features.facts,
            market_snapshot_ids=(
                current.snapshot_id,
                *(quote.snapshot_id for quote in fresh_chain),
            ),
        )
        await self._write(event, market, "candidate_packets", packet)

        async def audit(table: str, value: BaseModel | Mapping[str, Any]) -> None:
            await self._write(event, market, table, value)

        reasoning = await ReasoningPipeline(
            _AuditedProvider(self.provider, audit), self.clock, contract_selector=self.selector
        ).run(
            packet,
            policy_name=self.policy_name,
            policy_profile=self.policy_profile,
            trade_quality_score=quality.final_score,
            option_quotes=fresh_chain,
            underlying_price=current.last,
            contract_execution_score=execution_score,
        )
        for analysis in reasoning.analyses:
            await audit("agent_analyses", analysis)
        await audit("candidate_runs", {"reasoning_result": reasoning.model_dump(mode="json")})
        if (
            reasoning.policy_outcome is None
            or not reasoning.policy_outcome.proceed
            or reasoning.judge is None
            or reasoning.contract_selection is None
            or reasoning.judge.selected_candidate_rank is None
        ):
            return None, "reasoning_or_policy_abstained"
        quote = reasoning.contract_selection.ranked_quotes[
            reasoning.judge.selected_candidate_rank - 1
        ]
        expected_type = (
            OptionType.CALL
            if reasoning.judge.directional_thesis is Direction.BULLISH
            else OptionType.PUT
            if reasoning.judge.directional_thesis is Direction.BEARISH
            else None
        )
        if quote.contract.option_type is not expected_type:
            return None, "judge_contract_direction_mismatch"
        if not self._fresh(quote.metadata.effective_at, self.clock.now()):
            return None, "selected_quote_became_stale"
        proposal = TradeProposal(
            proposal_id=uuid5(
                NAMESPACE_URL, f"candidate:{self.binding.idempotency_namespace}:{event.event_id}"
            ),
            created_at=self.clock.now(),
            environment=self.binding.environment,
            namespace=self.binding.idempotency_namespace,
            packet_id=packet.packet_id,
            symbol=symbol,
            contract=quote.contract,
            side=OrderSide.BUY_TO_OPEN,
            quantity=1,
            limit_price=quote.ask,
            quote_snapshot_id=quote.snapshot_id,
            quote_as_of=quote.metadata.observed_at,
            policy_version=reasoning.policy_outcome.policy_version,
            risk_config_version=self.loaded.risk.version,
            thesis=reasoning.judge.thesis,
            invalidation_conditions=reasoning.judge.invalidation_conditions,
        )
        return proposal, "grounded_policy_pass_requires_execution_gates"

    def _measured_features(self, inputs: CandidateInputs) -> tuple[CandidateFeatureSet, bool]:
        quote, previous = inputs.current, inputs.previous
        facts = [self._fact("underlying_quote", quote.model_dump(mode="json"), quote)]
        facts.extend(inputs.verified_source_facts)
        facts.extend(inputs.federal_registry_facts)
        surveillance: dict[str, Decimal] = {}
        quality: dict[str, Decimal] = {}
        references: dict[str, tuple[str, ...]] = {}
        change: Decimal | None = None
        change_effective = quote.metadata.effective_at
        change_observed = quote.metadata.observed_at
        change_basis = ("prior_underlying_quote", "underlying_quote")
        if (
            previous is not None
            and previous.last > 0
            and previous.snapshot_id != quote.snapshot_id
            and previous.metadata.effective_at < quote.metadata.effective_at
            and previous.metadata.provider == quote.metadata.provider
            and previous.metadata.capability_version == quote.metadata.capability_version
            and quote.metadata.effective_at - previous.metadata.effective_at
            <= timedelta(seconds=self.limits.bar_lookback_seconds)
        ):
            change = (quote.last / previous.last - 1) * 100
            facts.append(
                self._fact("prior_underlying_quote", previous.model_dump(mode="json"), previous)
            )
        if len(inputs.bars) == 2:
            first, last = inputs.bars
            if (
                first.ends_at < last.ends_at
                and first.close > 0
                and self._fresh(last.ends_at, self.clock.now())
            ):
                if change is None:
                    change = (last.close / first.close - 1) * 100
                    change_effective = last.ends_at
                    change_observed = max(first.metadata.observed_at, last.metadata.observed_at)
                    change_basis = ("prior_bar", "current_bar")
                for name, bar in (("prior_bar", first), ("current_bar", last)):
                    facts.append(
                        CandidateFact(
                            fact_id=name,
                            value=bar.model_dump(mode="json"),
                            source_id=bar.metadata.source_id
                            or f"bar:{bar.symbol}:{bar.ends_at.isoformat()}",
                            effective_at=bar.ends_at,
                            observed_at=bar.metadata.observed_at,
                        )
                    )
                prior_range = first.high - first.low
                if prior_range > 0 and "technical_structure" in self.surveillance.weights.weights:
                    breakout = max(last.close - first.high, first.low - last.close, Decimal("0"))
                    surveillance["technical_structure"] = min(
                        Decimal("100"), breakout / prior_range * 100
                    )
                    references["surveillance:technical_structure"] = ("prior_bar", "current_bar")
        changed = change is not None and abs(change) >= self.limits.minimum_momentum_percent
        if change is not None:
            facts.append(
                CandidateFact(
                    fact_id="measured_price_change",
                    value={
                        "percent": str(change),
                        "basis_fact_ids": change_basis,
                        "formula": "(current_price / previous_price - 1) * 100",
                    },
                    source_id=f"derived:{sha256_json(change_basis)}",
                    effective_at=change_effective,
                    observed_at=change_observed,
                )
            )
            if "momentum_anomaly" in self.surveillance.weights.weights:
                surveillance["momentum_anomaly"] = min(
                    Decimal("100"), abs(change) / self.limits.full_scale_momentum_percent * 100
                )
                references["surveillance:momentum_anomaly"] = ("measured_price_change",)
        if "underlying_liquidity" in self.surveillance.weights.weights:
            surveillance["underlying_liquidity"] = min(
                Decimal("100"),
                quote.last * quote.volume / self.limits.full_scale_observed_dollar_volume * 100,
            )
            references["surveillance:underlying_liquidity"] = ("underlying_quote",)
        if (
            inputs.official_source_priority is not None
            and "catalyst_priority" in self.surveillance.weights.weights
        ):
            surveillance["catalyst_priority"] = inputs.official_source_priority
            references["surveillance:catalyst_priority"] = tuple(
                fact.fact_id for fact in inputs.verified_source_facts
            )
        if (
            inputs.federal_exposure_score is not None
            and "federal_exposure" in self.surveillance.weights.weights
        ):
            surveillance["federal_exposure"] = inputs.federal_exposure_score
            references["surveillance:federal_exposure"] = tuple(
                fact.fact_id for fact in inputs.federal_registry_facts
            )
        return CandidateFeatureSet(
            facts=tuple(facts),
            surveillance=surveillance,
            trade_quality=quality,
            component_fact_ids=references,
        ), changed

    def _fact(self, name: str, value: Any, quote: EquityQuote) -> CandidateFact:
        return CandidateFact(
            fact_id=name,
            value=value,
            source_id=str(quote.snapshot_id),
            effective_at=quote.metadata.effective_at,
            observed_at=quote.metadata.observed_at,
        )

    def _validate_features(self, features: CandidateFeatureSet, cutoff: datetime) -> None:
        ids = {fact.fact_id for fact in features.facts}
        if len(ids) != len(features.facts) or len(ids) > self.limits.maximum_facts:
            raise DataInvalidError("candidate fact IDs are duplicate or exceed budget")
        if len(features.model_dump_json().encode("utf-8")) > self.limits.maximum_feature_bytes:
            raise DataInvalidError("candidate feature bytes exceed budget")
        for fact in features.facts:
            self._available(fact.effective_at, cutoff)
            self._available(fact.observed_at, cutoff)
        for group, values in (
            ("surveillance", features.surveillance),
            ("trade_quality", features.trade_quality),
        ):
            for name, score in values.items():
                references = features.component_fact_ids.get(f"{group}:{name}", ())
                if not references or not set(references) <= ids or not Decimal(0) <= score <= 100:
                    raise DataInvalidError(
                        "scored candidate component lacks bounded factual provenance"
                    )

    async def _source_facts(
        self, event: SentinelEvent, symbol: str, cutoff: datetime
    ) -> tuple[tuple[CandidateFact, ...], Decimal | None]:
        configured = {source.id: source for source in self.loaded.sources.official_sources}
        facts: list[CandidateFact] = []
        priorities: list[Decimal] = []
        for reference in event.raw_reference_ids[: self.limits.maximum_source_documents]:
            row = await self.repository.find_payload("source_documents", "document_id", reference)
            if row is None:
                continue
            document = SourceDocument.model_validate(row["payload"])
            if self.binding.demo_backend is DemoBackend.OFFLINE_SIM:
                if document.data_mode == "LIVE_READ":
                    continue
            elif document.data_mode != "LIVE_READ":
                continue
            source = configured.get(document.source_id)
            if source is None:
                continue
            mapping_payload: dict[str, Any] | None = None
            observed_at = document.fetched_at
            if self.binding.demo_backend is DemoBackend.OFFLINE_SIM:
                if symbol not in {ticker.upper() for ticker in document.tickers}:
                    continue
            else:
                raw_mapping = event.payload.get("entity_mapping")
                try:
                    mapping = SourceEntityMapping.model_validate(raw_mapping)
                except Exception as exc:
                    raise DataInvalidError(
                        "official-source event lacks valid explicit entity mapping"
                    ) from exc
                expected = ExplicitIssuerMapper(self.loaded.sources).map(document)
                if (
                    mapping != expected
                    or mapping.status is not EntityMappingStatus.MAPPED
                    or mapping.mapped_ticker != symbol
                    or event.tickers != (symbol,)
                ):
                    raise DataInvalidError(
                        "official-source entity mapping is ambiguous, stale, or inconsistent"
                    )
                mapping_payload = mapping.model_dump(mode="json")
                observed_at = event.created_at
            url = urlsplit(document.canonical_url)
            if url.scheme != "https" or url.hostname != urlsplit(source.url).hostname:
                continue
            effective = document.publication_time or document.fetched_at
            self._available(effective, cutoff)
            self._available(document.fetched_at, cutoff)
            facts.append(
                CandidateFact(
                    fact_id=f"source:{document.document_id}",
                    value={
                        "title": document.title[:500],
                        "excerpt": document.normalized_text[
                            : self.limits.maximum_source_characters
                        ],
                        "url": document.canonical_url,
                        "content_hash": document.content_hash,
                        "source_config_version": self.loaded.sources.version,
                        "source_priority": str(source.surveillance_priority),
                        "entity_mapping": mapping_payload,
                    },
                    source_id=str(document.document_id),
                    effective_at=effective,
                    observed_at=observed_at,
                )
            )
            priorities.append(source.surveillance_priority)
        return tuple(facts), max(priorities) if priorities else None

    async def _federal_facts(
        self, symbol: str, cutoff: datetime
    ) -> tuple[tuple[CandidateFact, ...], Decimal | None]:
        if self.federal_registry is None:
            return (), None
        page = await self.federal_registry.snapshot(
            as_of=cutoff,
            ticker=symbol,
            limit=self.limits.maximum_federal_relationships,
        )
        if page.total_relationships > self.limits.maximum_federal_relationships:
            raise DataInvalidError("federal registry candidate fact budget exceeded")
        scored = await self.federal_registry.score(symbol, as_of=cutoff)
        item_ids = {item.revision.relationship_id for item in page.items}
        if item_ids != set(scored.included_relationship_ids) | set(
            scored.excluded_relationship_ids
        ):
            raise DataInvalidError("federal registry score and causal snapshot disagree")
        relationship_facts = tuple(
            CandidateFact(
                fact_id=(
                    f"federal:relationship:{item.revision.relationship_id}:"
                    f"{item.revision.revision_id}"
                ),
                value={
                    "relationship": item.revision.relationship.model_dump(mode="json"),
                    "revision_id": str(item.revision.revision_id),
                    "revision_hash": item.revision.revision_hash,
                    "evidence_status": item.evidence_status.value,
                    "eligible_for_research_score": item.eligible_for_research_score,
                },
                source_id=(
                    f"federal-registry:{item.revision.relationship_id}:{item.revision.revision_id}"
                ),
                effective_at=item.revision.created_at,
                observed_at=item.revision.created_at,
            )
            for item in page.items
            if item.eligible_for_research_score
        )
        aggregate = CandidateFact(
            fact_id=f"federal:score:{symbol}",
            value={
                **scored.model_dump(mode="json"),
                "relationship_fact_ids": [fact.fact_id for fact in relationship_facts],
            },
            source_id=f"federal-registry:{scored.policy_version}:{scored.score.version}",
            effective_at=cutoff,
            observed_at=cutoff,
        )
        return (aggregate, *relationship_facts), scored.score.value

    def _available(self, value: datetime, cutoff: datetime) -> None:
        if value.tzinfo is None or value.utcoffset() is None or value > cutoff:
            raise DataInvalidError("candidate evidence is not causally available")

    def _fresh(self, value: datetime, now: datetime) -> bool:
        return (
            timedelta(0)
            <= now - value
            <= timedelta(seconds=self.loaded.app.runtime.stale_market_data_seconds)
        )

    async def _write(
        self,
        event: SentinelEvent,
        market: MarketDataProvider,
        table: str,
        value: BaseModel | Mapping[str, Any],
    ) -> None:
        payload = value.model_dump(mode="json") if isinstance(value, BaseModel) else dict(value)
        await self.repository.append(
            table,
            {
                **payload,
                "created_at": self.clock.now(),
                "environment": self.binding.environment.value,
                "namespace": self.binding.idempotency_namespace,
                "run_id": self.run_id or uuid5(NAMESPACE_URL, f"candidate:{event.event_id}"),
                "source_event_id": str(event.event_id),
                "data_mode": "REPLAY" if market.capabilities.replay else "PROVIDER_DATA",
            },
        )

    async def _outcome(
        self,
        event: SentinelEvent,
        market: MarketDataProvider,
        status: str,
        reason: str,
        proposal: TradeProposal | None = None,
    ) -> None:
        await self._write(
            event,
            market,
            "candidate_runs",
            {
                "event_key": f"{self.binding.idempotency_namespace}:{event.event_id}",
                "event_hash": sha256_json(event.model_dump(mode="json")),
                "status": status,
                "reason": reason,
                "proposal": proposal.model_dump(mode="json") if proposal else None,
            },
        )
