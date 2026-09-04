from __future__ import annotations

import asyncio
from datetime import timedelta
from typing import Literal
from uuid import NAMESPACE_URL, uuid5

from pydantic import Field

from app.api.dashboard import RuntimeView
from app.broker.shadow_ledger import LedgerSnapshot
from app.broker.simulated import SimulatedBroker
from app.clock.base import Clock, VirtualClock
from app.config import LoadedConfig
from app.db.repository import InMemoryAuditRepository, PostgresAuditRepository
from app.domain.enums import (
    DemoBackend,
    ExecutionEnvironment,
    OrderSide,
    OrderState,
    RuntimeSafetyState,
    TradingMode,
)
from app.domain.models import (
    DomainModel,
    ExactApproval,
    OptionQuote,
    SentinelEvent,
    TradeProposal,
    sha256_json,
)
from app.exceptions import SafetyCriticalError
from app.execution.postgres_store import PostgresExecutionStore
from app.execution.service import DuplicateOrderError, ExecutionDenied, ExecutionService
from app.learning.outcomes import ClosedPositionReviewWorker
from app.market.models import ReplayFixture
from app.positions.manager import ExitPolicy, PositionManager
from app.risk.engine import RiskEngine
from app.sentinel.offline import OfflineReplayCheckpoint, OfflineReplaySession
from app.strategy.runtime import CandidateResearchWorker

AuditRepository = InMemoryAuditRepository | PostgresAuditRepository
OPEN_STATES = {
    OrderState.OPEN,
    OrderState.PARTIAL,
    OrderState.SUBMITTING,
    OrderState.SUBMISSION_UNKNOWN,
}


class OfflineRuntimeSnapshot(DomainModel):
    snapshot_kind: Literal["offline-runtime-v1"] = "offline-runtime-v1"
    environment: Literal["DEMO"] = "DEMO"
    demo_backend: Literal["OFFLINE_SIM"] = "OFFLINE_SIM"
    namespace: str
    checkpoint: OfflineReplayCheckpoint
    ledger: LedgerSnapshot
    payload_hash: str = Field(pattern=r"^[0-9a-f]{64}$")

    def validate_hash(self) -> None:
        expected = sha256_json(
            {
                "checkpoint": self.checkpoint.model_dump(mode="json"),
                "ledger": self.ledger.model_dump(mode="json"),
            }
        )
        if self.payload_hash != expected:
            raise SafetyCriticalError("offline runtime snapshot checksum mismatch")


class OfflineRuntime:
    """Durable, finite replay wired to the same execution service as other backends.

    The wall clock schedules work and measures host health. Only the virtual
    trading clock advances fixture data, quote ages, fills, and approval expiry.
    A completed fixture is never looped or represented as a current market feed.
    """

    def __init__(
        self,
        loaded: LoadedConfig,
        repository: AuditRepository,
        view: RuntimeView,
        wall_clock: Clock,
    ) -> None:
        self.loaded, self.repository, self.view = loaded, repository, view
        self.wall_clock = wall_clock
        self.binding = loaded.bind_runtime()
        if (
            self.binding.environment is not ExecutionEnvironment.DEMO
            or self.binding.demo_backend is not DemoBackend.OFFLINE_SIM
            or self.binding.external_write_authority
        ):
            raise SafetyCriticalError(
                "offline runtime requires a no-write DEMO/OFFLINE_SIM binding"
            )
        self._lock = asyncio.Lock()
        self._initialized = False
        self._checkpoint_persisted = True
        self._journal_healthy = False
        self._requires_reconciliation = True
        self.clock: VirtualClock
        self.broker: SimulatedBroker
        self.session: OfflineReplaySession
        self.store: PostgresExecutionStore
        self.execution: ExecutionService
        self.candidate_worker: CandidateResearchWorker | None = None

    @classmethod
    async def create(
        cls,
        loaded: LoadedConfig,
        repository: AuditRepository,
        view: RuntimeView,
        wall_clock: Clock,
        fixture: ReplayFixture,
    ) -> OfflineRuntime:
        runtime = cls(loaded, repository, view, wall_clock)
        row = await repository.find_payload(
            "shadow_ledger_events", "snapshot_kind", "offline-runtime-v1"
        )
        saved = (
            OfflineRuntimeSnapshot.model_validate(
                {key: value for key, value in row["payload"].items() if key != "created_at"}
            )
            if row
            else None
        )
        if saved is not None:
            saved.validate_hash()
            if saved.namespace != runtime.binding.idempotency_namespace:
                raise SafetyCriticalError("offline runtime snapshot namespace mismatch")
            initial_time = max(saved.checkpoint.replay_time, saved.ledger.recorded_at)
        else:
            if not fixture.records:
                raise ValueError("runtime replay fixture cannot be empty")
            initial_time = min(record.available_at for record in fixture.records)
        runtime.clock = VirtualClock(initial_time)
        runtime.store = PostgresExecutionStore(repository, runtime.clock)
        runtime.broker = SimulatedBroker(
            clock=runtime.clock,
            initial_cash=loaded.demo.initial_cash,
            fill_seed=loaded.demo.fill_seed,
            max_quote_age=timedelta(seconds=loaded.app.runtime.stale_market_data_seconds),
            namespace=runtime.binding.idempotency_namespace,
            state_recorder=runtime._persist_ledger,
            initial_state=saved.ledger if saved else None,
        )
        runtime.session = OfflineReplaySession(
            fixture,
            runtime.clock,
            repository,
            runtime._persist_checkpoint,
            namespace=runtime.binding.idempotency_namespace,
            quote_consumer=runtime._consume_quote,
            event_handler=runtime._event,
            checkpoint=saved.checkpoint if saved else None,
        )
        runtime.execution = ExecutionService(
            broker=runtime.broker,
            quotes=runtime.session.market,
            risk_engine=RiskEngine(
                loaded.risk,
                runtime.clock,
                maximum_quote_age=timedelta(seconds=loaded.app.runtime.stale_market_data_seconds),
                maximum_account_age=timedelta(
                    seconds=loaded.app.runtime.stale_account_data_seconds
                ),
            ),
            store=runtime.store,
            clock=runtime.clock,
            safety=view.safety,
            environment=runtime.binding.environment,
            namespace=runtime.binding.idempotency_namespace,
            trading_mode=lambda: view.trading_mode,
            kill_switch_active=lambda: (
                loaded.app.runtime.environment_execution_disabled
                or loaded.app.runtime.disabled_file.exists()
            ),
            healthcheck=lambda: (
                view.reconciled
                and view.database_healthy
                and runtime.broker.state_persisted
                and runtime._checkpoint_persisted
                and runtime._journal_healthy
                and not runtime._requires_reconciliation
                and not view.unresolved_submission
                and view.market_data_fresh
                and not runtime.session.complete
            ),
        )
        runtime._update_replay_view()
        return runtime

    async def _persist(self, ledger: LedgerSnapshot, checkpoint: OfflineReplayCheckpoint) -> None:
        self._checkpoint_persisted = False
        payload_hash = sha256_json(
            {
                "ledger": ledger.model_dump(mode="json"),
                "checkpoint": checkpoint.model_dump(mode="json"),
            }
        )
        envelope = OfflineRuntimeSnapshot(
            namespace=self.binding.idempotency_namespace,
            checkpoint=checkpoint,
            ledger=ledger,
            payload_hash=payload_hash,
        )
        await self.repository.append(
            "shadow_ledger_events",
            {
                **envelope.model_dump(mode="json"),
                "created_at": self.wall_clock.now(),
            },
        )
        self._checkpoint_persisted = True

    async def _persist_ledger(self, ledger: LedgerSnapshot) -> None:
        await self._persist(ledger, self.session.checkpoint)

    async def _persist_checkpoint(self, checkpoint: OfflineReplayCheckpoint) -> None:
        await self._persist(self.broker.export_state(), checkpoint)

    async def _event(self, event: SentinelEvent) -> None:
        # Events have already been durably journaled by the replay worker.
        self.view.last_scan_at = event.effective_at.isoformat()
        if self.candidate_worker is not None:
            proposal = await self.candidate_worker.on_event(event, self.session.market)
            if proposal is not None:
                await self.add_proposal(proposal)

    async def review_closed_positions(self) -> None:
        # Order state and fills are separate durable writes. Share the lifecycle
        # lock so the reviewer cannot mistake an in-progress sync for lost fills.
        async with self._lock:
            if not self._journal_healthy or self._requires_reconciliation:
                return
            await ClosedPositionReviewWorker(self.repository, self.clock).tick()

    async def _consume_quote(self, quote: OptionQuote) -> None:
        await self.broker.consume_quote(quote)
        # Replay may repeat an uncheckpointed group after a crash. Save ALL
        # known fills idempotently, including any persisted just before death.
        await self._sync_execution_journal(recovery=False)

    async def _sync_execution_journal(self, *, recovery: bool) -> None:
        self._journal_healthy = False
        state = self.broker.export_state()
        ledger_orders = {item.published.intent_id: item for item in state.orders}
        for item in state.orders:
            command = await self.store.get_command_for_order_intent(item.published.intent_id)
            intent = await self.store.get_order_intent(item.published.intent_id)
            if (
                intent is None
                or command is None
                or command.command_hash != item.command.command_hash
            ):
                raise SafetyCriticalError("ledger order lacks matching durable exact command")
            previous = await self.store.get_order(item.published.intent_id)
            if previous != item.published:
                await self.repository.append(
                    "reconciliation_events",
                    {
                        "created_at": self.clock.now(),
                        "environment": "DEMO",
                        "action": "RESTORE_LEDGER_ORDER" if recovery else "OBSERVE_LEDGER_ORDER",
                        "intent_id": str(item.published.intent_id),
                        "previous_state": previous.state.value if previous else None,
                        "current_state": item.published.state.value,
                        "ledger_hash": state.content_hash,
                    },
                )
                await self.store.save_order(item.published)
        for order in await self.store.list_latest_orders():
            intent = await self.store.get_order_intent(order.intent_id)
            # Cancellations refer to the original ledger order, not a new holding.
            if intent is not None and intent.action.value == "cancel_option_order":
                continue
            if order.intent_id not in ledger_orders:
                command = await self.store.get_command_for_order_intent(order.intent_id)
                transitions = await self.store.list_transitions(order.intent_id)
                if (
                    order.state is OrderState.REJECTED
                    and order.filled_quantity == 0
                    and order.broker_order_id is None
                    and intent is not None
                    and command is not None
                    and any(
                        transition.previous is OrderState.SUBMITTING
                        and transition.current is OrderState.REJECTED
                        and transition.reason
                        == ("local pre-submission gate denied; no broker write attempted")
                        for transition in transitions
                    )
                ):
                    continue
                raise SafetyCriticalError("durable order is missing from the simulated ledger")
        for fill in state.fills:
            await self.store.save_fill(fill)
        previous_positions = await self.store.list_positions()
        if tuple(previous_positions) != state.positions:
            await self.repository.append(
                "reconciliation_events",
                {
                    "created_at": self.clock.now(),
                    "environment": "DEMO",
                    "action": "RESTORE_LEDGER_POSITIONS"
                    if recovery
                    else "OBSERVE_LEDGER_POSITIONS",
                    "position_ids": [str(position.position_id) for position in state.positions],
                    "ledger_hash": state.content_hash,
                },
            )
            await self.store.replace_positions(state.positions)
        self._journal_healthy = True

    async def reconcile(self) -> bool:
        async with self._lock:
            self._requires_reconciliation = True
            if not self.broker.state_persisted or not self._checkpoint_persisted:
                if not await self.repository.healthcheck():
                    return False
                await self.broker.flush_state()
            await self._sync_execution_journal(recovery=True)
            self.view.unresolved_submission = bool(await self.store.unresolved_intents())
            report = await self.broker.reconcile()
            await self.store.save_reconciliation(report)
            self.view.observed_broker_account = report.observed_account
            self.view.effective_account = report.effective_account
            success = report.successful and not self.view.unresolved_submission
            if success and not self._initialized:
                await self._persist_checkpoint(self.session.checkpoint)
                self._initialized = True
            self._requires_reconciliation = not success
            return success

    async def health(self) -> bool:
        self.view.unresolved_submission = bool(await self.store.unresolved_intents())
        return (
            self._initialized
            and self._checkpoint_persisted
            and self._journal_healthy
            and not self._requires_reconciliation
            and self.broker.state_persisted
            and not self.view.unresolved_submission
        )

    def _update_replay_view(self) -> None:
        self.view.replay = {
            **self.session.checkpoint.model_dump(mode="json"),
            "trading_clock": self.clock.now().isoformat(),
            "wall_clock": self.wall_clock.now().isoformat(),
            "live_market_data": False,
        }
        self.view.market_data_fresh = not self.session.complete and self.session.freshness.is_fresh(
            "market_data", timedelta(seconds=self.loaded.app.runtime.stale_market_data_seconds)
        )
        if self.session.complete:
            self.view.safety.degrade(
                RuntimeSafetyState.ENTRY_DISABLED,
                "finite replay exhausted; no future fills available",
            )

    async def step(self) -> None:
        async with self._lock:
            if (
                not self._initialized
                or self._requires_reconciliation
                or not await self.repository.healthcheck()
            ):
                raise SafetyCriticalError("replay requires initialized durable state")
            try:
                await self.session.step()
                self._update_replay_view()
                await self._propose_exits()
            except BaseException:
                self._journal_healthy = False
                self._requires_reconciliation = True
                self.view.execution_service_healthy = False
                self.view.reconciled = False
                raise

    async def _propose_exits(self) -> None:
        manager = PositionManager(self.clock, ExitPolicy(version="exit-policy-v1"))
        open_orders = [
            order for order in await self.broker.get_orders() if order.state in OPEN_STATES
        ]
        for position in await self.broker.get_positions():
            if any(
                order.contract == position.contract and order.side is OrderSide.SELL_TO_CLOSE
                for order in open_orders
            ):
                continue
            quote = await self.session.market.get_option_quote(position.contract.instrument_id)
            decision = manager.evaluate_exit(position, quote)
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
            raise SafetyCriticalError("proposal startup binding mismatch")
        existing = await self.repository.find_payload(
            "trade_proposals", "proposal_id", str(proposal.proposal_id)
        )
        if existing is None:
            await self.repository.append("trade_proposals", proposal)
        elif TradeProposal.model_validate(existing["payload"]) != proposal:
            raise SafetyCriticalError("proposal identity reused with different content")
        self.view.proposals[proposal.proposal_id] = proposal

    async def dispatch_proposals(self) -> None:
        async with self._lock:
            try:
                cursor: int | None = None
                for _ in range(200):
                    rows = await self.repository.list_payloads(
                        "trade_proposals", limit=500, before_sequence=cursor
                    )
                    for row in rows:
                        proposal = TradeProposal.model_validate(row["payload"])
                        await self._dispatch(proposal)
                    if len(rows) < 500:
                        break
                    cursor = min(row["append_sequence"] for row in rows)
                else:
                    raise SafetyCriticalError(
                        "proposal dispatch exceeded its complete-history bound"
                    )
            except BaseException:
                self._journal_healthy = False
                self._requires_reconciliation = True
                self.view.execution_service_healthy = False
                self.view.reconciled = False
                raise

    async def _dispatch(self, proposal: TradeProposal) -> None:
        if self.session.complete:
            return
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
        except DuplicateOrderError:
            pass
        except ExecutionDenied as exc:
            await self.repository.append(
                "environment_audit_events",
                {
                    "created_at": self.clock.now(),
                    "environment": "DEMO",
                    "action": "PROPOSAL_EXECUTION_DENIED",
                    "proposal_id": str(proposal.proposal_id),
                    "reason": str(exc),
                },
            )
            return
        self.view.proposals.pop(proposal.proposal_id, None)
