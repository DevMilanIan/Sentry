from __future__ import annotations

import asyncio
from collections.abc import Mapping
from datetime import timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any
from uuid import UUID, uuid5

from pydantic import BaseModel

from app.broker.simulated import SimulatedBroker
from app.clock.base import VirtualClock
from app.config import LoadedConfig, load_config
from app.db.repository import InMemoryAuditRepository
from app.domain.enums import (
    AttentionLevel,
    Direction,
    ExecutionEnvironment,
    JudgeDecision,
    OrderSide,
    OrderState,
    SelectorStatus,
    TradingMode,
)
from app.domain.models import DomainModel, TradeProposal, sha256_json
from app.exceptions import DataInvalidError, SafetyCriticalError
from app.execution.service import ExecutionResult, ExecutionService, InMemoryExecutionStore
from app.market.models import ReplayFixture
from app.market.replay import LookaheadViolationError, OfflineReplayMarketDataProvider
from app.options.selector import ContractSelector, ContractSelectorConfig
from app.positions.manager import ExitPolicy, ExitTrigger, PositionManager
from app.reasoning.pipeline import PipelineStatus, ReasoningPipeline
from app.reasoning.policies import load_decision_policy_set
from app.reasoning.provider import LocalModelProvider, ReasoningRole
from app.reasoning.schemas import (
    BearAnalysis,
    BullAnalysis,
    JudgeAnalysis,
    SituationAnalysis,
    SkepticAnalysis,
)
from app.reasoning.scripted import ScriptedReplayModelProvider
from app.risk.engine import RiskEngine
from app.safety.runtime_state import SafetyController, SafetyEvidence
from app.strategy.attention import AttentionMapper, AttentionStage, AttentionThresholds
from app.strategy.candidates import CandidateFact, CandidatePacketBuilder
from app.strategy.scoring import SurveillanceScorer, TradeQualityScorer

SCENARIO_VERSION = "offline-demo-lifecycle-v1"
SCENARIO_NAMESPACE = UUID("d42e4299-7a63-5cd5-9a53-06360c1dde22")
DEFAULT_FIXTURE = Path(__file__).parents[1] / "market" / "fixtures" / "offline_e2e_session.json"


class OfflineScenarioResult(DomainModel):
    scenario_version: str
    run_id: UUID
    environment: ExecutionEnvironment
    namespace: str
    fixture_version: str
    model_name: str
    reasoning_status: str
    reasoning_roles: tuple[str, ...]
    policy_proceeded: bool
    selected_instrument_id: str
    rejected_instrument_ids: tuple[str, ...]
    entry_submission_state: OrderState
    entry_final_state: OrderState
    exit_submission_state: OrderState
    exit_final_state: OrderState
    entry_fill_price: Decimal
    exit_fill_price: Decimal
    realized_pnl: Decimal
    final_cash: Decimal
    final_positions: int
    final_open_orders: int
    fill_seeds: tuple[int, ...]
    no_lookahead_proven: bool
    same_event_fill_blocked: bool
    audit_counts: dict[str, int]
    semantic_journal_hash: str


async def run_offline_demo_scenario(
    config_dir: Path = Path("config"),
    *,
    fixture_path: Path = DEFAULT_FIXTURE,
    loaded: LoadedConfig | None = None,
    model_provider: LocalModelProvider | None = None,
    repository: InMemoryAuditRepository | None = None,
) -> OfflineScenarioResult:
    """Run one bounded, fully causal Demo AUTO entry/exit lifecycle.

    The default reasoning provider is a versioned scripted replay model so this
    function is an exact regression oracle.  Ollama is measured independently;
    callers may inject it here for exploratory, non-deterministic Demo runs.
    """

    configuration = loaded or load_config(config_dir)
    binding = configuration.bind_runtime()
    if (
        binding.environment is not ExecutionEnvironment.DEMO
        or binding.demo_backend is None
        or binding.demo_backend.value != "OFFLINE_SIM"
        or binding.external_write_authority
    ):
        raise SafetyCriticalError(
            "offline scenario requires immutable DEMO/OFFLINE_SIM with no external writes"
        )

    fixture_text = await asyncio.to_thread(fixture_path.read_text, encoding="utf-8")
    fixture = ReplayFixture.model_validate_json(fixture_text)
    if len(fixture.records) < 6:
        raise DataInvalidError("offline E2E fixture lacks the complete lifecycle timeline")
    records = tuple(sorted(fixture.records, key=lambda item: item.sequence))
    clock = VirtualClock(records[0].available_at)
    market = OfflineReplayMarketDataProvider(fixture, clock)
    audit = repository or InMemoryAuditRepository(binding)
    run_id = uuid5(SCENARIO_NAMESPACE, f"{SCENARIO_VERSION}:{fixture.version}")

    await _append(
        audit,
        "system_runs",
        {
            "created_at": clock.now(),
            "run_id": run_id,
            "environment": binding.environment.value,
            "scenario_version": SCENARIO_VERSION,
            "status": "STARTED",
            "external_write_authority": False,
        },
        run_id,
        clock,
    )

    # Asking for the first option observation while the clock is still at the
    # equity event must be rejected rather than revealing future fixture state.
    no_lookahead_proven = False
    try:
        await market.get_option_chain("ACME", as_of=records[1].available_at)
    except LookaheadViolationError:
        no_lookahead_proven = True
    if not no_lookahead_proven:
        raise SafetyCriticalError("market provider disclosed a future option quote")

    equity = await market.get_equity_quote("ACME")
    await _append(audit, "market_snapshots", equity, run_id, clock)
    await clock.advance_to(records[1].available_at)
    chain = tuple(await market.get_option_chain("ACME"))
    for quote in chain:
        await _append(audit, "option_snapshots", quote, run_id, clock)

    surveillance = SurveillanceScorer(
        version=f"{configuration.strategy.version}:surveillance",
        weights=configuration.strategy.scoring.surveillance,
    ).score(
        {
            "catalyst_priority": 95,
            "momentum_anomaly": 78,
            "technical_structure": 75,
            "market_sector_alignment": 70,
            "underlying_liquidity": 90,
            "federal_exposure": 72,
            "event_urgency": 88,
        }
    )
    thresholds = configuration.strategy.attention_thresholds
    attention = AttentionMapper(
        AttentionThresholds(
            version=configuration.strategy.version,
            watch=thresholds.l1_watch,
            candidate=thresholds.l2_candidate,
            trade_worthy=thresholds.l3_trade_worthy,
            deep_research=thresholds.l4_deep_research,
        )
    ).map(surveillance.final_score, stage=AttentionStage.SURVEILLANCE)
    if attention is not AttentionLevel.CANDIDATE:
        raise SafetyCriticalError("surveillance may allocate attention but cannot declare a trade")

    facts = (
        CandidateFact(
            fact_id="catalyst:award",
            value="Primary-source fixture reports a signed strategic program award.",
            source_id="fixture:primary:catalyst",
            effective_at=records[0].effective_at,
            observed_at=records[0].observed_at,
        ),
        CandidateFact(
            fact_id="market:last",
            value=str(equity.last),
            source_id=equity.metadata.source_id or "fixture:equity",
            effective_at=equity.metadata.effective_at,
            observed_at=equity.metadata.observed_at,
        ),
        CandidateFact(
            fact_id="market:volume",
            value=equity.volume,
            source_id=equity.metadata.source_id or "fixture:equity",
            effective_at=equity.metadata.effective_at,
            observed_at=equity.metadata.observed_at,
        ),
        CandidateFact(
            fact_id="federal:exposure",
            value="Verified direct program exposure score 72/100.",
            source_id="fixture:federal-registry",
            effective_at=records[0].effective_at,
            observed_at=records[0].observed_at,
        ),
    )
    packet = CandidatePacketBuilder(clock).build(
        run_id=run_id,
        symbol="ACME",
        attention=attention,
        surveillance_score=surveillance.final_score,
        facts=facts,
        market_snapshot_ids=(equity.snapshot_id, *(quote.snapshot_id for quote in chain)),
    )
    await _append(audit, "candidate_features", surveillance, run_id, clock)
    await _append(audit, "candidate_packets", packet, run_id, clock)

    option_config = configuration.strategy.options
    selector = ContractSelector(
        ContractSelectorConfig.from_risk_config(
            configuration.risk,
            minimum_dte=option_config.minimum_dte,
            maximum_dte=option_config.maximum_dte,
            maximum_absolute_moneyness_percent=(option_config.maximum_absolute_moneyness_percent),
            maximum_candidates=option_config.maximum_candidates_for_judge,
            version=f"{configuration.strategy.version}:selector",
        ),
        clock,
    )
    selection_detail = selector.evaluate(Direction.BULLISH, equity.last, chain)
    selection = selection_detail.selection
    if selection.status is not SelectorStatus.CONTRACT_FOUND:
        raise SafetyCriticalError("E2E fixture did not produce an affordable liquid contract")
    for evaluation in selection_detail.evaluations:
        await _append(audit, "contract_candidates", evaluation, run_id, clock)
    selected = selection.ranked_quotes[0]
    selected_evaluation = next(
        item
        for item in selection_detail.evaluations
        if item.quote.snapshot_id == selected.snapshot_id
    )

    trade_quality = TradeQualityScorer(
        version=f"{configuration.strategy.version}:trade-quality",
        weights=configuration.strategy.scoring.trade_quality,
    ).score(
        {
            "catalyst_materiality": 90,
            "adversarial_evidence": 78,
            "contract_execution": selected_evaluation.quality_score,
            "timing_confirmation": 82,
            "payoff_plausibility": 76,
            "market_sector_alignment": 70,
        }
    )
    provider = model_provider or ScriptedReplayModelProvider(_scripted_outputs())
    policies = load_decision_policy_set(config_dir / "decision_policies.yaml")
    reasoning = await ReasoningPipeline(provider, clock).run(
        packet,
        policy_name="DEMO_EXPLORATORY",
        policy_profile=policies.profiles["DEMO_EXPLORATORY"],
        trade_quality_score=trade_quality.final_score,
        contract_selection=selection,
        force_skeptic=True,
        deterministic_rules_passed=True,
        contract_execution_score=selected_evaluation.quality_score,
    )
    for analysis in reasoning.analyses:
        await _append(audit, "model_calls", analysis, run_id, clock)
        await _append(audit, "agent_analyses", analysis, run_id, clock)
    if (
        reasoning.status is not PipelineStatus.JUDGED
        or reasoning.policy_outcome is None
        or not reasoning.policy_outcome.proceed
        or reasoning.judge is None
    ):
        raise SafetyCriticalError("reasoning/policy did not authorize the hypothetical lifecycle")
    await _append(audit, "decision_policy_versions", reasoning.policy_outcome, run_id, clock)

    proposal = TradeProposal(
        proposal_id=uuid5(SCENARIO_NAMESPACE, f"{run_id}:entry"),
        created_at=clock.now(),
        environment=ExecutionEnvironment.DEMO,
        namespace=binding.idempotency_namespace,
        packet_id=packet.packet_id,
        symbol=packet.symbol,
        contract=selected.contract,
        side=OrderSide.BUY_TO_OPEN,
        quantity=1,
        limit_price=selected.ask,
        quote_snapshot_id=selected.snapshot_id,
        quote_as_of=selected.metadata.observed_at,
        policy_version=reasoning.policy_outcome.policy_version,
        risk_config_version=configuration.risk.version,
        thesis=reasoning.judge.thesis,
        invalidation_conditions=reasoning.judge.invalidation_conditions,
    )
    await _append(audit, "trade_proposals", proposal, run_id, clock)

    safety = _normal_safety(clock)
    broker = SimulatedBroker(
        clock=clock,
        initial_cash=configuration.demo.initial_cash,
        fill_seed=configuration.demo.fill_seed,
        max_quote_age=timedelta(seconds=configuration.app.runtime.stale_market_data_seconds),
        namespace=binding.idempotency_namespace,
    )
    await broker.consume_quote(selected)
    store = InMemoryExecutionStore()
    execution = ExecutionService(
        broker=broker,
        quotes=market,
        risk_engine=RiskEngine(
            configuration.risk,
            clock,
            maximum_quote_age=timedelta(
                seconds=configuration.app.runtime.stale_market_data_seconds
            ),
            maximum_account_age=timedelta(
                seconds=configuration.app.runtime.stale_account_data_seconds
            ),
        ),
        store=store,
        clock=clock,
        safety=safety,
        environment=ExecutionEnvironment.DEMO,
        namespace=binding.idempotency_namespace,
        trading_mode=TradingMode.AUTO,
    )
    entry = await execution.execute_entry(proposal)
    await _persist_execution(audit, entry, run_id, clock)
    if entry.broker_order.state is not OrderState.OPEN:
        raise SafetyCriticalError("entry must remain open until a post-order quote arrives")
    same_event_fill_blocked = not await broker.consume_quote(selected)
    if not same_event_fill_blocked:
        raise SafetyCriticalError("submission-time quote incorrectly filled the order")

    await clock.advance_to(records[3].available_at)
    entry_market = await market.get_option_quote(selected.contract.instrument_id)
    await _append(audit, "option_snapshots", entry_market, run_id, clock)
    entry_fills = await broker.consume_quote(entry_market)
    entry_order = _order_for_intent(await broker.get_orders(), entry.order_intent.intent_id)
    entry_positions = await broker.get_positions()
    await execution.record_broker_update(
        entry_order,
        fills=entry_fills,
        positions=entry_positions,
    )
    await _persist_broker_update(audit, entry_order, entry_fills, entry_positions, run_id, clock)
    if len(entry_fills) != 1 or entry_order.state is not OrderState.FILLED:
        raise SafetyCriticalError("post-order entry quote did not produce one complete fill")
    if len(entry_positions) != 1:
        raise SafetyCriticalError("entry fill did not create exactly one long position")

    await clock.advance_to(records[4].available_at)
    profit_market = await market.get_option_quote(selected.contract.instrument_id)
    await _append(audit, "option_snapshots", profit_market, run_id, clock)
    if await broker.consume_quote(profit_market):
        raise SafetyCriticalError("profit mark unexpectedly filled a terminal entry order")
    position = (await broker.get_positions())[0]
    exit_manager = PositionManager(
        clock,
        ExitPolicy(version="exit-policy-v1", profit_target_fraction=Decimal("0.50")),
    )
    exit_decision = exit_manager.evaluate_exit(position, profit_market)
    if (
        not exit_decision.executable
        or exit_decision.limit_price is None
        or ExitTrigger.PROFIT_TARGET not in exit_decision.triggers
    ):
        raise SafetyCriticalError("deterministic profit exit was not triggered")
    await _append(
        audit,
        "position_snapshots",
        {
            "created_at": clock.now(),
            "environment": ExecutionEnvironment.DEMO.value,
            "position": position.model_dump(mode="json"),
            "exit_decision": _dataclass_payload(exit_decision),
        },
        run_id,
        clock,
    )

    exit_proposal = TradeProposal(
        proposal_id=uuid5(SCENARIO_NAMESPACE, f"{run_id}:exit"),
        created_at=clock.now(),
        environment=ExecutionEnvironment.DEMO,
        namespace=binding.idempotency_namespace,
        packet_id=packet.packet_id,
        symbol=packet.symbol,
        contract=position.contract,
        side=OrderSide.SELL_TO_CLOSE,
        quantity=position.quantity,
        limit_price=exit_decision.limit_price,
        quote_snapshot_id=profit_market.snapshot_id,
        quote_as_of=profit_market.metadata.observed_at,
        policy_version=exit_decision.policy_version,
        risk_config_version=configuration.risk.version,
        thesis="deterministic risk-reducing exit",
        invalidation_conditions=position.invalidation_conditions,
    )
    await _append(audit, "trade_proposals", exit_proposal, run_id, clock)
    exit_result = await execution.execute(exit_proposal)
    await _persist_execution(audit, exit_result, run_id, clock)
    if exit_result.broker_order.state is not OrderState.OPEN:
        raise SafetyCriticalError("exit must remain open until a post-order quote arrives")

    await clock.advance_to(records[5].available_at)
    exit_market = await market.get_option_quote(selected.contract.instrument_id)
    await _append(audit, "option_snapshots", exit_market, run_id, clock)
    exit_fills = await broker.consume_quote(exit_market)
    exit_order = _order_for_intent(await broker.get_orders(), exit_result.order_intent.intent_id)
    final_positions = await broker.get_positions()
    await execution.record_broker_update(
        exit_order,
        fills=exit_fills,
        positions=final_positions,
    )
    await _persist_broker_update(audit, exit_order, exit_fills, final_positions, run_id, clock)
    if len(exit_fills) != 1 or exit_order.state is not OrderState.FILLED:
        raise SafetyCriticalError("post-order exit quote did not produce one complete fill")
    if final_positions:
        raise SafetyCriticalError("complete sell-to-close left a simulated position")

    reconciliation = await broker.reconcile()
    await store.save_reconciliation(reconciliation)
    await _append(audit, "reconciliation_events", reconciliation, run_id, clock)
    final_account = await broker.get_effective_execution_account_state()
    if (
        not reconciliation.successful
        or reconciliation.position_count
        or reconciliation.open_order_count
        or final_account.cash != Decimal("31.00")
        or broker.ledger.realized_pnl != Decimal("6.00")
    ):
        raise SafetyCriticalError("final simulated account/reconciliation invariant failed")

    all_fills = (*entry_fills, *exit_fills)
    semantic_journal = {
        "scenario_version": SCENARIO_VERSION,
        "fixture_version": fixture.version,
        "run_id": str(run_id),
        "config_versions": {
            "app": configuration.app.version,
            "risk": configuration.risk.version,
            "strategy": configuration.strategy.version,
            "demo": configuration.demo.version,
        },
        "packet_hash": packet.content_hash,
        "analyses": tuple((item.role, item.output_hash) for item in reasoning.analyses),
        "selection": tuple(quote.contract.instrument_id for quote in selection.ranked_quotes),
        "rejections": selection.rejected_reasons,
        "policy": {
            "version": reasoning.policy_outcome.policy_version,
            "decision": reasoning.policy_outcome.effective_decision.value,
            "failed": reasoning.policy_outcome.failed_requirements,
        },
        "commands": (
            {
                "intent_id": str(entry.order_intent.intent_id),
                "order_id": str(entry_order.order_id),
                "arguments": entry.command_intent.validated_arguments,
            },
            {
                "intent_id": str(exit_result.order_intent.intent_id),
                "order_id": str(exit_order.order_id),
                "arguments": exit_result.command_intent.validated_arguments,
            },
        ),
        "fills": tuple(
            {
                "order_id": str(fill.order_id),
                "quantity": fill.quantity,
                "price": str(fill.price),
                "event_ids": fill.market_event_ids,
                "model": fill.fill_model_version,
                "seed": fill.deterministic_seed,
            }
            for fill in all_fills
        ),
        "exit_triggers": tuple(trigger.value for trigger in exit_decision.triggers),
        "final": {
            "cash": str(final_account.cash),
            "realized_pnl": str(broker.ledger.realized_pnl),
            "positions": reconciliation.position_count,
            "open_orders": reconciliation.open_order_count,
        },
        "causality": {
            "no_lookahead": no_lookahead_proven,
            "same_event_fill_blocked": same_event_fill_blocked,
        },
    }
    await _append(
        audit,
        "trade_outcomes",
        {
            "created_at": clock.now(),
            "environment": ExecutionEnvironment.DEMO.value,
            "symbol": packet.symbol,
            "realized_pnl": str(broker.ledger.realized_pnl),
            "semantic_journal_hash": sha256_json(semantic_journal),
        },
        run_id,
        clock,
    )
    await _append(
        audit,
        "system_runs",
        {
            "created_at": clock.now(),
            "environment": ExecutionEnvironment.DEMO.value,
            "status": "COMPLETE",
            "semantic_journal_hash": sha256_json(semantic_journal),
        },
        run_id,
        clock,
    )
    tracked_tables = (
        "system_runs",
        "market_snapshots",
        "option_snapshots",
        "candidate_features",
        "candidate_packets",
        "model_calls",
        "agent_analyses",
        "contract_candidates",
        "risk_decisions",
        "trade_proposals",
        "broker_reviews",
        "order_intents",
        "broker_command_intents",
        "orders",
        "fills",
        "positions",
        "position_snapshots",
        "trade_outcomes",
        "reconciliation_events",
    )
    audit_counts = {table: len(await audit.list(table, limit=10_000)) for table in tracked_tables}
    return OfflineScenarioResult(
        scenario_version=SCENARIO_VERSION,
        run_id=run_id,
        environment=ExecutionEnvironment.DEMO,
        namespace=binding.idempotency_namespace,
        fixture_version=fixture.version,
        model_name=provider.model_name,
        reasoning_status=reasoning.status.value,
        reasoning_roles=tuple(item.role for item in reasoning.analyses),
        policy_proceeded=reasoning.policy_outcome.proceed,
        selected_instrument_id=selected.contract.instrument_id,
        rejected_instrument_ids=tuple(sorted(selection.rejected_reasons)),
        entry_submission_state=entry.broker_order.state,
        entry_final_state=entry_order.state,
        exit_submission_state=exit_result.broker_order.state,
        exit_final_state=exit_order.state,
        entry_fill_price=entry_fills[0].price,
        exit_fill_price=exit_fills[0].price,
        realized_pnl=broker.ledger.realized_pnl,
        final_cash=final_account.cash,
        final_positions=reconciliation.position_count,
        final_open_orders=reconciliation.open_order_count,
        fill_seeds=tuple(fill.deterministic_seed for fill in all_fills),
        no_lookahead_proven=no_lookahead_proven,
        same_event_fill_blocked=same_event_fill_blocked,
        audit_counts=audit_counts,
        semantic_journal_hash=sha256_json(semantic_journal),
    )


def _scripted_outputs() -> Mapping[ReasoningRole, BaseModel]:
    fact_ids = (
        "catalyst:award",
        "federal:exposure",
        "market:last",
        "market:volume",
    )
    return {
        ReasoningRole.SITUATION: SituationAnalysis(
            materiality=Decimal("0.84"),
            directional_bias=Direction.BULLISH,
            time_horizon="five to ten sessions",
            primary_driver="verified program award",
            supporting_facts=fact_ids,
            uncertainties=("follow-through timing",),
            thesis_invalidation_conditions=("award is withdrawn",),
            research_needed=(),
            abstain_reason=None,
        ),
        ReasoningRole.BULL: BullAnalysis(
            thesis="A verified award can reprice the small underlying.",
            supporting_fact_ids=("catalyst:award", "market:volume"),
            upside_drivers=("program revenue relevance",),
            required_evidence=("continued volume confirmation",),
            failure_conditions=("award is withdrawn",),
            confidence=Decimal("0.72"),
        ),
        ReasoningRole.BEAR: BearAnalysis(
            downside_thesis="The initial response can fade despite the award.",
            supporting_fact_ids=("catalyst:award", "market:last"),
            downside_drivers=("catalyst may already be priced",),
            bullish_option_failure_modes=("theta decay", "liquidity spread"),
            required_evidence=("post-event price follow-through",),
            confidence=Decimal("0.42"),
        ),
        ReasoningRole.SKEPTIC: SkepticAnalysis(
            hidden_assumptions=("award economics reach the issuer quickly",),
            stale_or_circular_evidence=(),
            catalyst_priced_in_arguments=("the opening move may reflect the award",),
            timing_challenges=("repricing may outlast option holding window",),
            contract_economics_challenges=("spread and theta can erase a small move",),
            thesis_right_but_option_loses=("underlying rises too slowly",),
            decision_changing_data=("award withdrawal or volume failure",),
            referenced_fact_ids=fact_ids,
            unresolved_primary_source_conflict=False,
        ),
        ReasoningRole.JUDGE: JudgeAnalysis(
            decision=JudgeDecision.PASS,
            directional_thesis=Direction.BULLISH,
            selected_candidate_rank=1,
            confidence=Decimal("0.72"),
            expected_time_window="five to ten sessions",
            catalyst_strength=Decimal("0.84"),
            contract_quality_critique="Affordable with a nonzero bid and bounded spread.",
            thesis="Verified award plus liquid execution supports a bounded Demo entry.",
            invalidation_conditions=("award is withdrawn",),
            recheck_conditions=("volume fails or spread widens",),
            reasons_to_abstain=(),
            rationale="The frozen evidence and adversarial review support Demo exploration.",
            referenced_fact_ids=fact_ids,
            evidence_complete=True,
            capital_opportunity_cost_addressed=False,
        ),
    }


def _normal_safety(clock: VirtualClock) -> SafetyController:
    safety = SafetyController(clock, timedelta(0))
    evidence = SafetyEvidence(
        database_writable=True,
        broker_state_known=True,
        reconciled=True,
        market_data_fresh=True,
        account_data_fresh=True,
        execution_service_healthy=True,
        kill_switch_clear=True,
        environment_matches=True,
    )
    safety.observe(evidence)
    safety.observe(evidence)
    return safety


async def _persist_execution(
    repository: InMemoryAuditRepository,
    result: ExecutionResult,
    run_id: UUID,
    clock: VirtualClock,
) -> None:
    await _append(repository, "risk_decisions", result.risk_decision, run_id, clock)
    await _append(repository, "broker_reviews", result.review, run_id, clock)
    await _append(repository, "order_intents", result.order_intent, run_id, clock)
    await _append(repository, "broker_command_intents", result.command_intent, run_id, clock)
    await _append(repository, "orders", result.broker_order, run_id, clock)


async def _persist_broker_update(
    repository: InMemoryAuditRepository,
    order: BaseModel,
    fills: tuple[BaseModel, ...],
    positions: tuple[BaseModel, ...],
    run_id: UUID,
    clock: VirtualClock,
) -> None:
    await _append(repository, "orders", order, run_id, clock)
    for fill in fills:
        await _append(repository, "fills", fill, run_id, clock)
    for position in positions:
        await _append(repository, "positions", position, run_id, clock)


async def _append(
    repository: InMemoryAuditRepository,
    table: str,
    value: BaseModel | dict[str, Any],
    run_id: UUID,
    clock: VirtualClock,
) -> None:
    payload = value.model_dump(mode="json") if isinstance(value, BaseModel) else dict(value)
    payload.setdefault("created_at", clock.now().isoformat())
    payload.setdefault("run_id", str(run_id))
    await repository.append(table, payload)


def _order_for_intent(orders: tuple[Any, ...], intent_id: UUID) -> Any:
    try:
        return next(order for order in orders if order.intent_id == intent_id)
    except StopIteration as exc:
        raise SafetyCriticalError("broker lost an order intent during the scenario") from exc


def _dataclass_payload(value: Any) -> dict[str, Any]:
    return {
        "position_id": str(value.position_id),
        "evaluated_at": value.evaluated_at.isoformat(),
        "should_exit": value.should_exit,
        "executable": value.executable,
        "side": value.side.value,
        "quantity": value.quantity,
        "limit_price": str(value.limit_price) if value.limit_price is not None else None,
        "triggers": tuple(trigger.value for trigger in value.triggers),
        "unrealized_pnl": str(value.unrealized_pnl),
        "unrealized_return": str(value.unrealized_return),
        "quote_snapshot_id": str(value.quote_snapshot_id),
        "policy_version": value.policy_version,
        "reason": value.reason,
    }
