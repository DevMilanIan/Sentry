"""Credential-independent composition for the authenticated broker-shadow runtime."""

from __future__ import annotations

import asyncio
from collections.abc import Mapping, Sequence
from datetime import timedelta
from typing import Any, Literal, Protocol
from uuid import NAMESPACE_URL, UUID, uuid5

from pydantic import BaseModel, Field

from app.api.dashboard import RuntimeView
from app.broker.robinhood_mcp import RobinhoodReadReviewClient
from app.broker.shadow import BrokerShadowReviewEvidence, RobinhoodShadowBroker
from app.broker.shadow_ledger import LedgerSnapshot
from app.clock.base import Clock
from app.config import LoadedConfig, RuntimeBinding
from app.domain.enums import (
    BrokerAction,
    DemoBackend,
    ExecutionEnvironment,
    FirewallDisposition,
    OrderSide,
    OrderState,
    TradingMode,
)
from app.domain.models import (
    ExactApproval,
    FirewallDecision,
    OptionQuote,
    SentinelEvent,
    TimestampedModel,
    TradeProposal,
    sha256_json,
)
from app.exceptions import SafetyCriticalError
from app.execution.postgres_store import PostgresExecutionStore
from app.execution.service import (
    CommandArgumentBuilder,
    DuplicateOrderError,
    ExecutionDenied,
    ExecutionService,
)
from app.market.base import MarketDataProvider
from app.positions.manager import ExitPolicy, PositionManager
from app.reasoning.provider import LocalModelProvider
from app.risk.engine import RiskEngine
from app.safety.write_firewall import DenyAllWriteFirewall
from app.sentinel.live_reads import LiveReadSurveillanceWorker
from app.strategy.live_research import LiveMarketResearchQueue
from app.strategy.runtime import CandidateResearchWorker


class ShadowAuditRepository(Protocol):
    binding: RuntimeBinding

    async def healthcheck(self) -> bool: ...

    async def append(self, table: str, value: BaseModel | Mapping[str, Any]) -> UUID: ...

    async def find_payload(self, table: str, key: str, value: Any) -> dict[str, Any] | None: ...

    async def list_payloads(
        self,
        table: str,
        *,
        filters: Mapping[str, Any] | None = None,
        limit: int = 1000,
        before_sequence: int | None = None,
    ) -> list[dict[str, Any]]: ...


class BrokerShadowRuntimeSnapshot(TimestampedModel):
    """Append-only restart image bound to one external account identity."""

    snapshot_kind: Literal["broker-shadow-runtime-v1"] = "broker-shadow-runtime-v1"
    environment: Literal["DEMO"] = "DEMO"
    demo_backend: Literal["BROKER_SHADOW"] = "BROKER_SHADOW"
    namespace: str = Field(min_length=1)
    account_fingerprint: str = Field(min_length=1)
    ledger: LedgerSnapshot
    payload_hash: str = Field(pattern=r"^[0-9a-f]{64}$")

    def validate_hash(self) -> None:
        expected = sha256_json(
            {
                "environment": self.environment,
                "demo_backend": self.demo_backend,
                "namespace": self.namespace,
                "account_fingerprint": self.account_fingerprint,
                "ledger": self.ledger.model_dump(mode="json"),
            }
        )
        if self.payload_hash != expected:
            raise SafetyCriticalError("broker-shadow runtime snapshot checksum mismatch")


class _ShadowExecutionQuoteProvider:
    """Feeds exact current quotes through the ledger before execution consumes them."""

    def __init__(self, runtime: BrokerShadowRuntime) -> None:
        self._runtime = runtime

    async def get_option_quote(self, instrument_id: str) -> OptionQuote:
        quote = await self._runtime.market.get_option_quote(instrument_id)
        await self._runtime._consume_execution_quote(quote)
        return quote


class BrokerShadowRuntime:
    """One DEMO/BROKER_SHADOW composition; construction performs no authentication.

    The caller injects a capability-specific authenticated read/review facade and
    a current (never replay) market provider.  Only ``RobinhoodShadowBroker`` is
    given to ``ExecutionService``.  That broker contains no generic MCP transport,
    so its place/cancel methods can only persist an exact command, await the
    deny-all firewall audit, and mutate this runtime's local ShadowLedger.
    """

    def __init__(
        self,
        loaded: LoadedConfig,
        repository: ShadowAuditRepository,
        view: RuntimeView,
        clock: Clock,
        *,
        read_client: RobinhoodReadReviewClient,
        market: MarketDataProvider,
        model_provider: LocalModelProvider,
        market_watchlist: Sequence[str],
        expected_account_fingerprint: str,
        saved: BrokerShadowRuntimeSnapshot | None,
        command_argument_builder: CommandArgumentBuilder | None,
        acknowledged_historical_order_ids: frozenset[str],
    ) -> None:
        self.loaded, self.repository, self.view, self.clock = loaded, repository, view, clock
        self.binding = loaded.bind_runtime()
        self.market = market
        self.expected_account_fingerprint = expected_account_fingerprint
        self._guard(market_watchlist)
        if saved is not None:
            saved.validate_hash()
            if (
                saved.namespace != self.binding.idempotency_namespace
                or saved.account_fingerprint != expected_account_fingerprint
            ):
                raise SafetyCriticalError(
                    "broker-shadow restart does not match the qualified account/namespace"
                )

        self.store = PostgresExecutionStore(repository, clock)
        self._operation_lock = asyncio.Lock()
        self._dispatch_lock = asyncio.Lock()
        self._initialized = False
        self._requires_reconciliation = True
        self._journal_healthy = False
        self._snapshot_persisted = saved is not None

        firewall = DenyAllWriteFirewall(clock, self._record_firewall_decision)
        self.broker = RobinhoodShadowBroker(
            read_client=read_client,
            clock=clock,
            initial_cash=loaded.broker_shadow.initial_cash,
            fill_seed=loaded.broker_shadow.fill_seed,
            max_quote_age=timedelta(seconds=loaded.app.runtime.stale_market_data_seconds),
            namespace=self.binding.idempotency_namespace,
            firewall=firewall,
            command_recorder=self.store.save_command_intent,
            state_recorder=self._persist_ledger,
            review_recorder=self._record_review_evidence,
            initial_state=saved.ledger if saved is not None else None,
            expected_account_fingerprint=expected_account_fingerprint,
            acknowledged_historical_order_ids=acknowledged_historical_order_ids,
        )
        self.surveillance = LiveReadSurveillanceWorker(
            market, clock, repository, watchlist=market_watchlist
        )
        self.researcher = CandidateResearchWorker(
            loaded,
            clock,
            repository,
            model_provider,
            policy_profile=loaded.decision_policies.profiles["DEMO_EXPLORATORY"],
        )
        self.research_queue = LiveMarketResearchQueue(
            repository, clock, market, self.researcher
        )
        self.position_manager = PositionManager(clock, ExitPolicy(version="exit-policy-v1"))
        self.execution = ExecutionService(
            broker=self.broker,
            quotes=_ShadowExecutionQuoteProvider(self),
            risk_engine=RiskEngine(
                loaded.risk,
                clock,
                maximum_quote_age=timedelta(
                    seconds=loaded.app.runtime.stale_market_data_seconds
                ),
                maximum_account_age=timedelta(
                    seconds=loaded.app.runtime.stale_account_data_seconds
                ),
            ),
            store=self.store,
            clock=clock,
            safety=view.safety,
            environment=self.binding.environment,
            namespace=self.binding.idempotency_namespace,
            trading_mode=lambda: view.trading_mode,
            kill_switch_active=lambda: (
                loaded.app.runtime.environment_execution_disabled
                or loaded.app.runtime.disabled_file.exists()
            ),
            healthcheck=self._execution_healthcheck,
            command_argument_builder=command_argument_builder,
        )

    @classmethod
    async def create(
        cls,
        loaded: LoadedConfig,
        repository: ShadowAuditRepository,
        view: RuntimeView,
        clock: Clock,
        *,
        read_client: RobinhoodReadReviewClient,
        market: MarketDataProvider,
        model_provider: LocalModelProvider,
        market_watchlist: Sequence[str],
        expected_account_fingerprint: str,
        command_argument_builder: CommandArgumentBuilder | None = None,
        acknowledged_historical_order_ids: frozenset[str] = frozenset(),
    ) -> BrokerShadowRuntime:
        """Restore local state only; the first explicit reconcile performs MCP reads."""

        if not expected_account_fingerprint.strip():
            raise ValueError("the selected Agentic account fingerprint is required")
        row = await repository.find_payload(
            "shadow_ledger_events", "snapshot_kind", "broker-shadow-runtime-v1"
        )
        saved = BrokerShadowRuntimeSnapshot.model_validate(row["payload"]) if row else None
        return cls(
            loaded,
            repository,
            view,
            clock,
            read_client=read_client,
            market=market,
            model_provider=model_provider,
            market_watchlist=market_watchlist,
            expected_account_fingerprint=expected_account_fingerprint,
            saved=saved,
            command_argument_builder=command_argument_builder,
            acknowledged_historical_order_ids=acknowledged_historical_order_ids,
        )

    def _guard(self, market_watchlist: Sequence[str]) -> None:
        if (
            self.binding.environment is not ExecutionEnvironment.DEMO
            or self.binding.demo_backend is not DemoBackend.BROKER_SHADOW
            or self.binding.external_write_authority
            or self.repository.binding != self.binding
            or self.view.binding != self.binding
            or self.view.write_firewall != "DENY_ALL_WRITES"
        ):
            raise SafetyCriticalError(
                "broker-shadow runtime requires immutable no-write DEMO/BROKER_SHADOW bindings"
            )
        if not market_watchlist:
            raise ValueError("broker-shadow requires an explicit current-market watchlist")
        capabilities = self.market.capabilities
        if capabilities.replay or not capabilities.option_quotes:
            raise SafetyCriticalError("broker-shadow cannot substitute replay market data")

    async def _persist_ledger(self, ledger: LedgerSnapshot) -> None:
        self._snapshot_persisted = False
        values = {
            "environment": "DEMO",
            "demo_backend": "BROKER_SHADOW",
            "namespace": self.binding.idempotency_namespace,
            "account_fingerprint": self.expected_account_fingerprint,
            "ledger": ledger.model_dump(mode="json"),
        }
        snapshot = BrokerShadowRuntimeSnapshot(
            created_at=self.clock.now(),
            **values,
            payload_hash=sha256_json(values),
        )
        await self.repository.append("shadow_ledger_events", snapshot)
        self._snapshot_persisted = True

    async def _record_review_evidence(self, evidence: BrokerShadowReviewEvidence) -> None:
        if (
            evidence.namespace != self.binding.idempotency_namespace
            or evidence.proposal_id != evidence.shadow_review.proposal_id
            or evidence.broker_observed_review is not None
            and evidence.proposal_id != evidence.broker_observed_review.proposal_id
        ):
            raise SafetyCriticalError("broker-shadow review evidence crossed its exact proposal")
        await self.repository.append("broker_reviews", evidence)

    async def _record_firewall_decision(self, decision: FirewallDecision) -> None:
        if (
            decision.environment is not ExecutionEnvironment.DEMO
            or decision.disposition is not FirewallDisposition.BLOCKED_SHADOW
            or decision.transmitted
        ):
            raise SafetyCriticalError("broker-shadow firewall did not return a durable denial")
        command = await self.store.get_command_intent(decision.command_intent_id)
        if command is None:
            raise SafetyCriticalError(
                "write firewall cannot deny a command that lacks a durable exact intent"
            )
        await self.repository.append(
            "external_write_firewall_events",
            {
                **decision.model_dump(mode="json"),
                "namespace": self.binding.idempotency_namespace,
                "record_kind": "broker_shadow_external_write_denial_v1",
                "command_hash": command.command_hash,
                "idempotency_key": command.idempotency_key,
                "capability_name": command.capability_name,
            },
        )

    def _execution_healthcheck(self) -> bool:
        return (
            self._initialized
            and not self._requires_reconciliation
            and self._journal_healthy
            and self._snapshot_persisted
            and self.broker.state_persisted
            and self.view.database_healthy
            and self.view.market_data_fresh
            and self.view.reconciled
            and not self.view.unresolved_submission
        )

    async def _sync_execution_journal(self, *, recovery: bool) -> None:
        self._journal_healthy = False
        state = self.broker.export_state()
        ledger_orders = {item.published.intent_id: item for item in state.orders}
        for item in state.orders:
            intent = await self.store.get_order_intent(item.published.intent_id)
            command = await self.store.get_command_for_order_intent(item.published.intent_id)
            if (
                intent is None
                or command is None
                or command.command_hash != item.command.command_hash
            ):
                raise SafetyCriticalError("shadow ledger order lacks its durable exact command")
            previous = await self.store.get_order(item.published.intent_id)
            if previous != item.published:
                await self.repository.append(
                    "reconciliation_events",
                    {
                        "created_at": self.clock.now(),
                        "environment": "DEMO",
                        "namespace": self.binding.idempotency_namespace,
                        "action": (
                            "RESTORE_SHADOW_LEDGER_ORDER"
                            if recovery
                            else "OBSERVE_SHADOW_LEDGER_ORDER"
                        ),
                        "intent_id": str(item.published.intent_id),
                        "previous_state": previous.state.value if previous else None,
                        "current_state": item.published.state.value,
                        "ledger_hash": state.content_hash,
                    },
                )
                await self.store.save_order(item.published)

        for order in await self.store.list_latest_orders():
            intent = await self.store.get_order_intent(order.intent_id)
            if intent is not None and intent.action is BrokerAction.CANCEL_OPTION_ORDER:
                continue
            if order.intent_id not in ledger_orders:
                command = await self.store.get_command_for_order_intent(order.intent_id)
                transitions = await self.store.list_transitions(order.intent_id)
                locally_rejected = (
                    order.state is OrderState.REJECTED
                    and order.filled_quantity == 0
                    and order.broker_order_id is None
                    and intent is not None
                    and command is not None
                    and any(
                        transition.previous is OrderState.SUBMITTING
                        and transition.current is OrderState.REJECTED
                        and "no broker write attempted" in transition.reason
                        for transition in transitions
                    )
                )
                if not locally_rejected:
                    raise SafetyCriticalError(
                        "durable shadow order is absent from the append-only ledger"
                    )

        for fill in state.fills:
            await self.store.save_fill(fill)
        if await self.store.list_positions() != state.positions:
            await self.store.replace_positions(state.positions)
        self._journal_healthy = True

    async def reconcile(self) -> bool:
        """Verify real $0 state, account continuity, local ledger, and execution journal."""

        async with self._operation_lock:
            self._requires_reconciliation = True
            self.view.reconciled = False
            self.view.database_healthy = await self.repository.healthcheck()
            if not self.view.database_healthy:
                self.view.execution_service_healthy = False
                return False
            if not self.broker.state_persisted:
                await self.broker.flush_state()
            capabilities = await self.broker.get_capabilities()
            await self.repository.append(
                "broker_capability_snapshots",
                {
                    **capabilities.model_dump(mode="json"),
                    "created_at": self.clock.now(),
                    "environment": "DEMO",
                    "namespace": self.binding.idempotency_namespace,
                },
            )
            report = await self.broker.reconcile()
            if (
                not capabilities.execution_ready
                or capabilities.external_writes_enabled
                or not capabilities.review_option_orders
            ):
                report = report.model_copy(
                    update={
                        "successful": False,
                        "discrepancies": report.discrepancies
                        + ("broker read/review/write-schema capabilities are not ready",),
                    }
                )
            if report.successful and not self._snapshot_persisted:
                # A fresh account binding becomes durable only after the actual
                # selected identity and zero-account conditions reconcile.
                await self.broker.flush_state()
            await self.store.save_reconciliation(report)
            await self.repository.append(
                "broker_observed_account_snapshots", report.observed_account
            )
            await self.repository.append("shadow_account_snapshots", report.effective_account)
            self.view.observed_broker_account = report.observed_account
            self.view.effective_account = report.effective_account
            self.view.broker_connected = (
                report.observed_account.is_authenticated and report.observed_account.state_known
            )
            if report.successful:
                await self._sync_execution_journal(recovery=True)
            else:
                self._journal_healthy = False
            self.view.unresolved_submission = bool(await self.store.unresolved_intents())
            success = (
                report.successful
                and self._journal_healthy
                and not self.view.unresolved_submission
            )
            self._initialized = self._initialized or success
            self._requires_reconciliation = not success
            self.view.reconciled = success
            await self._refresh_view_from_ledger()
            self.view.execution_service_healthy = self._execution_healthcheck()
            return success

    async def health(self) -> bool:
        self.view.database_healthy = await self.repository.healthcheck()
        self.view.unresolved_submission = bool(await self.store.unresolved_intents())
        self.view.execution_service_healthy = self._execution_healthcheck()
        return self.view.execution_service_healthy

    async def scan_current_market(self) -> tuple[SentinelEvent, ...]:
        """Persist a current-data scan, then advance only existing shadow lifecycles."""

        try:
            events = await self.surveillance.tick()
            self.view.market_data_fresh = await self.surveillance.health()
            self.view.last_scan_at = self.clock.now().isoformat()
            await self.monitor_shadow_positions()
            await self.health()
            return events
        except BaseException:
            self.view.market_data_fresh = False
            self.view.execution_service_healthy = False
            raise

    async def research_pending_market_event(self) -> TradeProposal | None:
        proposal = await self.research_queue.tick()
        if proposal is not None:
            await self.add_proposal(proposal)
        return proposal

    async def _consume_execution_quote(self, quote: OptionQuote) -> None:
        await self.broker.consume_quote(quote)
        await self._sync_execution_journal(recovery=False)

    async def monitor_shadow_positions(self) -> None:
        """Refresh open hypothetical instruments; it never queries a real write surface."""

        async with self._operation_lock:
            if not self._initialized or self._requires_reconciliation:
                raise SafetyCriticalError(
                    "shadow position monitoring requires successful account reconciliation"
                )
            orders = await self.broker.get_orders()
            positions = await self.broker.get_positions()
            instruments = {
                order.contract.instrument_id
                for order in orders
                if order.state in {OrderState.OPEN, OrderState.PARTIAL}
            }
            instruments.update(position.contract.instrument_id for position in positions)
            for instrument_id in sorted(instruments):
                quote = await self.market.get_option_quote(instrument_id)
                await self._consume_execution_quote(quote)
            await self._propose_exits()
            await self._refresh_view_from_ledger()
            self.view.execution_service_healthy = self._execution_healthcheck()

    async def _refresh_view_from_ledger(self) -> None:
        positions = await self.broker.get_positions()
        orders = await self.broker.get_orders()
        self.view.effective_account = await self.broker.get_effective_execution_account_state()
        self.view.positions = [item.model_dump(mode="json") for item in positions]
        self.view.open_orders = [
            item.model_dump(mode="json")
            for item in orders
            if item.state in {OrderState.OPEN, OrderState.PARTIAL}
        ]

    async def _propose_exits(self) -> None:
        orders = await self.broker.get_orders()
        open_orders = [
            order for order in orders if order.state in {OrderState.OPEN, OrderState.PARTIAL}
        ]
        for position in await self.broker.get_positions():
            if any(
                order.contract == position.contract and order.side is OrderSide.SELL_TO_CLOSE
                for order in open_orders
            ):
                continue
            quote = await self.market.get_option_quote(position.contract.instrument_id)
            decision = self.position_manager.evaluate_exit(position, quote)
            if not decision.executable or decision.limit_price is None:
                continue
            proposal = TradeProposal(
                proposal_id=uuid5(
                    NAMESPACE_URL,
                    f"exit:{self.binding.idempotency_namespace}:"
                    f"{position.position_id}:{quote.snapshot_id}",
                ),
                created_at=self.clock.now(),
                environment=self.binding.environment,
                namespace=self.binding.idempotency_namespace,
                packet_id=uuid5(NAMESPACE_URL, f"position:{position.position_id}"),
                symbol=position.contract.symbol,
                contract=position.contract,
                side=OrderSide.SELL_TO_CLOSE,
                quantity=position.quantity,
                limit_price=decision.limit_price,
                quote_snapshot_id=quote.snapshot_id,
                quote_as_of=quote.metadata.observed_at,
                policy_version=decision.policy_version,
                risk_config_version=self.loaded.risk.version,
                thesis=decision.reason,
                invalidation_conditions=position.invalidation_conditions,
            )
            await self.add_proposal(proposal)

    async def add_proposal(self, proposal: TradeProposal) -> None:
        if (
            proposal.environment is not self.binding.environment
            or proposal.namespace != self.binding.idempotency_namespace
        ):
            raise SafetyCriticalError("proposal does not match the broker-shadow startup binding")
        existing = await self.repository.find_payload(
            "trade_proposals", "proposal_id", str(proposal.proposal_id)
        )
        if existing is None:
            await self.repository.append("trade_proposals", proposal)
        elif TradeProposal.model_validate(existing["payload"]) != proposal:
            raise SafetyCriticalError("proposal identity was reused with different content")
        self.view.proposals[proposal.proposal_id] = proposal

    async def dispatch_proposals(self) -> None:
        async with self._dispatch_lock, self._operation_lock:
            cursor: int | None = None
            for _ in range(200):
                rows = await self.repository.list_payloads(
                    "trade_proposals", limit=500, before_sequence=cursor
                )
                for row in rows:
                    await self._dispatch(TradeProposal.model_validate(row["payload"]))
                if len(rows) < 500:
                    return
                cursor = min(int(row["append_sequence"]) for row in rows)
            raise SafetyCriticalError("broker-shadow proposal history exceeded its bounded scan")

    async def _dispatch(self, proposal: TradeProposal) -> None:
        approval_row = await self.repository.find_payload(
            "approvals", "proposal_id", str(proposal.proposal_id)
        )
        approval = ExactApproval.model_validate(approval_row["payload"]) if approval_row else None
        existing = await self.store.find_intent_by_fingerprint(proposal.order_fingerprint)
        if existing is not None or approval is not None and approval.rejected:
            self.view.proposals.pop(proposal.proposal_id, None)
            return
        self.view.proposals[proposal.proposal_id] = proposal
        mode = self.view.trading_mode
        if mode is TradingMode.RESEARCH:
            return
        if mode is TradingMode.APPROVAL or (
            mode is TradingMode.EXIT_AUTO and proposal.side is OrderSide.BUY_TO_OPEN
        ):
            if approval is None or not approval.is_valid_for(proposal, self.clock.now()):
                return
        if not self.view.execution_service_healthy or not self.view.reconciled:
            return
        try:
            await self.execution.execute(proposal, approval=approval)
            await self._sync_execution_journal(recovery=False)
        except DuplicateOrderError:
            pass
        except ExecutionDenied as exc:
            evidence = self.broker.last_review_evidence
            if (
                evidence is not None
                and evidence.observed_state_before_hash is not None
                and evidence.observed_state_after_hash is not None
                and not evidence.observed_state_unchanged
            ):
                self.view.safety.emergency_stop(
                    "broker-observed state changed across a purportedly safe review"
                )
                self._requires_reconciliation = True
                self.view.reconciled = False
                self.view.execution_service_healthy = False
            await self.repository.append(
                "environment_audit_events",
                {
                    "created_at": self.clock.now(),
                    "environment": "DEMO",
                    "namespace": self.binding.idempotency_namespace,
                    "action": "BROKER_SHADOW_PROPOSAL_EXECUTION_DENIED",
                    "proposal_id": str(proposal.proposal_id),
                    "reason_type": type(exc).__name__,
                },
            )
            return
        self.view.proposals.pop(proposal.proposal_id, None)
        await self._refresh_view_from_ledger()
