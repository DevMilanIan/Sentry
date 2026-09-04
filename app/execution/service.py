from __future__ import annotations

import asyncio
import math
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Protocol
from uuid import UUID, uuid5

from app.broker.base import BrokerAccountExecution, ReconciliationReport
from app.clock.base import Clock
from app.domain.enums import (
    AccountKind,
    BrokerAction,
    ExecutionEnvironment,
    OrderSide,
    OrderState,
    TradingMode,
)
from app.domain.models import (
    BrokerCommandIntent,
    BrokerOrder,
    BrokerReview,
    ExactApproval,
    Fill,
    OptionQuote,
    OrderIntent,
    Position,
    RiskDecision,
    TradeProposal,
    sha256_json,
)
from app.exceptions import SafetyCriticalError, SubmissionUnknownError
from app.risk.engine import RiskEngine
from app.safety.runtime_state import SafetyController

EXECUTION_ID_NAMESPACE = UUID("4afef107-9024-5b74-a307-236994188f16")


class ExecutionDenied(SafetyCriticalError):
    """A deterministic execution gate denied the exact proposal."""


class DuplicateOrderError(ExecutionDenied):
    """The proposal fingerprint already has a durable intent."""


class QuoteProvider(Protocol):
    async def get_option_quote(self, instrument_id: str) -> OptionQuote: ...


CommandArgumentBuilder = Callable[[TradeProposal, str], dict[str, Any]]


@dataclass(frozen=True, slots=True)
class StateTransitionRecord:
    intent_id: UUID
    previous: OrderState
    current: OrderState
    reason: str
    recorded_at: datetime
    reconciliation_evidence: bool = False


@dataclass(frozen=True, slots=True)
class ExecutionResult:
    order_intent: OrderIntent
    command_intent: BrokerCommandIntent
    risk_decision: RiskDecision
    review: BrokerReview
    broker_order: BrokerOrder


@dataclass(frozen=True, slots=True)
class CancellationResult:
    order_intent: OrderIntent
    command_intent: BrokerCommandIntent
    broker_order: BrokerOrder


@dataclass(frozen=True, slots=True)
class SubmissionReconciliation:
    intent_id: UUID
    matched_order: BrokerOrder | None
    broker_report: object
    negative_match_proven: bool
    retry_permitted: bool


class ExecutionStore(Protocol):
    async def save_risk_decision(self, decision: RiskDecision) -> None: ...

    async def save_review(self, review: BrokerReview) -> None: ...

    async def save_order_intent(self, intent: OrderIntent) -> None: ...

    async def save_command_intent(self, command: BrokerCommandIntent) -> None: ...

    async def save_order(self, order: BrokerOrder) -> None: ...

    async def save_fill(self, fill: Fill) -> None: ...

    async def replace_positions(self, positions: tuple[Position, ...]) -> None: ...

    async def save_reconciliation(self, report: ReconciliationReport) -> None: ...

    async def record_transition(self, record: StateTransitionRecord) -> None: ...

    async def find_intent_by_fingerprint(self, fingerprint: str) -> OrderIntent | None: ...

    async def get_order(self, intent_id: UUID) -> BrokerOrder | None: ...

    async def get_order_intent(self, intent_id: UUID) -> OrderIntent | None: ...

    async def get_command_for_order_intent(self, intent_id: UUID) -> BrokerCommandIntent | None: ...

    async def record_negative_reconciliation(self, intent_id: UUID) -> None: ...


class InMemoryExecutionStore:
    """Deterministic test/reference store with uniqueness equivalent to DB constraints."""

    def __init__(self) -> None:
        self.risk_decisions: dict[UUID, RiskDecision] = {}
        self.reviews: dict[UUID, BrokerReview] = {}
        self.order_intents: dict[UUID, OrderIntent] = {}
        self.command_intents: dict[UUID, BrokerCommandIntent] = {}
        self.orders: dict[UUID, BrokerOrder] = {}
        self.fills: dict[UUID, Fill] = {}
        self.positions: dict[UUID, Position] = {}
        self.reconciliations: list[ReconciliationReport] = []
        self.transitions: list[StateTransitionRecord] = []
        self.negative_reconciliations: set[UUID] = set()
        self.audit_sequence: list[tuple[str, UUID]] = []
        self._fingerprints: dict[str, UUID] = {}
        self._idempotency_keys: dict[str, UUID] = {}
        self._lock = asyncio.Lock()

    async def save_risk_decision(self, decision: RiskDecision) -> None:
        async with self._lock:
            self.risk_decisions.setdefault(decision.decision_id, decision)
            self.audit_sequence.append(("risk_decision", decision.decision_id))

    async def save_review(self, review: BrokerReview) -> None:
        async with self._lock:
            self.reviews.setdefault(review.review_id, review)
            self.audit_sequence.append(("broker_review", review.review_id))

    async def save_order_intent(self, intent: OrderIntent) -> None:
        async with self._lock:
            if intent.intent_id in self.order_intents:
                raise DuplicateOrderError("order intent id already exists")
            if intent.order_fingerprint in self._fingerprints:
                raise DuplicateOrderError("order fingerprint already has a durable intent")
            if intent.idempotency_key in self._idempotency_keys:
                raise DuplicateOrderError("idempotency key already has a durable intent")
            self.order_intents[intent.intent_id] = intent
            self._fingerprints[intent.order_fingerprint] = intent.intent_id
            self._idempotency_keys[intent.idempotency_key] = intent.intent_id
            self.audit_sequence.append(("order_intent", intent.intent_id))

    async def save_command_intent(self, command: BrokerCommandIntent) -> None:
        async with self._lock:
            if command.command_intent_id in self.command_intents:
                raise DuplicateOrderError("command intent id already exists")
            if command.order_intent_id not in self.order_intents:
                raise SafetyCriticalError("command cannot precede its durable order intent")
            self.command_intents[command.command_intent_id] = command
            self.audit_sequence.append(("broker_command_intent", command.command_intent_id))

    async def save_order(self, order: BrokerOrder) -> None:
        async with self._lock:
            if order.intent_id not in self.order_intents:
                raise SafetyCriticalError("broker order cannot precede its durable order intent")
            self.orders[order.intent_id] = order
            self.audit_sequence.append(("broker_order", order.order_id))

    async def save_fill(self, fill: Fill) -> None:
        async with self._lock:
            if fill.fill_id in self.fills:
                return
            if not any(order.order_id == fill.order_id for order in self.orders.values()):
                raise SafetyCriticalError("fill cannot precede its durable broker order")
            self.fills[fill.fill_id] = fill
            self.audit_sequence.append(("fill", fill.fill_id))

    async def replace_positions(self, positions: tuple[Position, ...]) -> None:
        async with self._lock:
            environments = {position.environment for position in positions}
            if len(environments) > 1:
                raise SafetyCriticalError("position snapshot mixes execution environments")
            self.positions = {position.position_id: position for position in positions}
            marker = uuid5(
                EXECUTION_ID_NAMESPACE,
                "positions:" + sha256_json([item.model_dump(mode="json") for item in positions]),
            )
            self.audit_sequence.append(("positions_reconciled", marker))

    async def save_reconciliation(self, report: ReconciliationReport) -> None:
        async with self._lock:
            self.reconciliations.append(report)
            marker = uuid5(
                EXECUTION_ID_NAMESPACE,
                "reconciliation:" + sha256_json(report.model_dump(mode="json")),
            )
            self.audit_sequence.append(("reconciliation", marker))

    async def record_transition(self, record: StateTransitionRecord) -> None:
        async with self._lock:
            self.transitions.append(record)
            self.audit_sequence.append(("state_transition", record.intent_id))

    async def find_intent_by_fingerprint(self, fingerprint: str) -> OrderIntent | None:
        async with self._lock:
            intent_id = self._fingerprints.get(fingerprint)
            return self.order_intents.get(intent_id) if intent_id else None

    async def get_order(self, intent_id: UUID) -> BrokerOrder | None:
        async with self._lock:
            return self.orders.get(intent_id)

    async def get_order_intent(self, intent_id: UUID) -> OrderIntent | None:
        async with self._lock:
            return self.order_intents.get(intent_id)

    async def get_command_for_order_intent(self, intent_id: UUID) -> BrokerCommandIntent | None:
        async with self._lock:
            return next(
                (
                    command
                    for command in self.command_intents.values()
                    if command.order_intent_id == intent_id
                ),
                None,
            )

    async def record_negative_reconciliation(self, intent_id: UUID) -> None:
        async with self._lock:
            self.negative_reconciliations.add(intent_id)
            self.audit_sequence.append(("negative_reconciliation", intent_id))


class ExecutionService:
    """The sole orchestration path allowed to ask a broker to mutate order state."""

    def __init__(
        self,
        *,
        broker: BrokerAccountExecution,
        quotes: QuoteProvider,
        risk_engine: RiskEngine,
        store: ExecutionStore,
        clock: Clock,
        safety: SafetyController,
        environment: ExecutionEnvironment,
        namespace: str,
        trading_mode: TradingMode | Callable[[], TradingMode],
        kill_switch_active: Callable[[], bool] = lambda: False,
        healthcheck: Callable[[], bool] = lambda: True,
        command_argument_builder: CommandArgumentBuilder | None = None,
        cancellation_audit_timeout_seconds: float = 5.0,
    ) -> None:
        if not namespace:
            raise ValueError("execution namespace is required")
        if (
            not math.isfinite(cancellation_audit_timeout_seconds)
            or not 0 < cancellation_audit_timeout_seconds <= 10
        ):
            raise ValueError(
                "cancellation audit timeout must be finite and between 0 and 10 seconds"
            )
        self._broker = broker
        self._quotes = quotes
        self._risk = risk_engine
        self._store = store
        self._clock = clock
        self._safety = safety
        self._environment = environment
        self._namespace = namespace
        self._trading_mode = trading_mode
        self._kill_switch_active = kill_switch_active
        self._healthcheck = healthcheck
        self._argument_builder = command_argument_builder or self._default_arguments
        self._execution_lock = asyncio.Lock()
        self._cancellation_audit_timeout_seconds = cancellation_audit_timeout_seconds
        self._interrupted_write_intents: set[UUID] = set()

    @property
    def interrupted_write_intents(self) -> frozenset[UUID]:
        """Process-local fail-closed latch; durable records survive process restart."""
        return frozenset(self._interrupted_write_intents)

    async def execute(
        self,
        proposal: TradeProposal,
        *,
        approval: ExactApproval | None = None,
    ) -> ExecutionResult:
        # Serial admission prevents two proposals from spending the same
        # pre-submission buying power before the broker reserves it.
        async with self._execution_lock:
            return await self._execute(proposal, approval=approval)

    async def _execute(
        self, proposal: TradeProposal, *, approval: ExactApproval | None = None
    ) -> ExecutionResult:
        mode = self._trading_mode() if callable(self._trading_mode) else self._trading_mode
        self._validate_binding(proposal)

        duplicate = await self._store.find_intent_by_fingerprint(proposal.order_fingerprint)
        if duplicate is not None:
            order = await self._store.get_order(duplicate.intent_id)
            if order is not None and order.state is OrderState.SUBMISSION_UNKNOWN:
                raise SubmissionUnknownError(
                    "matching submission is unresolved; reconciliation is required "
                    "and blind retry is forbidden"
                )
            raise DuplicateOrderError(
                "matching order intent already exists; duplicate submission blocked"
            )
        self._preflight(proposal, mode)

        effective_account = await self._broker.get_effective_execution_account_state()
        observed_account = await self._broker.get_observed_broker_account_state()
        reconciliation = await self._broker.reconcile()
        await self._store.save_reconciliation(reconciliation)
        if not reconciliation.successful:
            raise ExecutionDenied("broker reconciliation is incomplete")

        quote = await self._quotes.get_option_quote(proposal.contract.instrument_id)
        risk = self._risk.evaluate(proposal, effective_account, quote)
        await self._store.save_risk_decision(risk)
        if not risk.allowed:
            raise ExecutionDenied("hard risk denial: " + ",".join(risk.failed_rules))

        review = await self._broker.review_option_order(proposal)
        await self._store.save_review(review)
        if not review.side_effect_free:
            raise ExecutionDenied("broker review was not proven side-effect-free")
        if not review.accepted:
            raise ExecutionDenied("broker pre-trade review rejected the exact proposal")

        self._validate_approval(proposal, mode, approval)
        # Review and persistence may take long enough for quote/account data
        # or the operator's mode/kill switch to change. Revalidate before intent.
        quote = await self._quotes.get_option_quote(proposal.contract.instrument_id)
        effective_account = await self._broker.get_effective_execution_account_state()
        risk = self._risk.evaluate(proposal, effective_account, quote)
        await self._store.save_risk_decision(risk)
        if not risk.allowed:
            raise ExecutionDenied("post-review hard risk denial: " + ",".join(risk.failed_rules))
        current_mode = self._trading_mode() if callable(self._trading_mode) else self._trading_mode
        if current_mode != mode:
            raise ExecutionDenied("trading mode changed during review")
        self._preflight(proposal, mode)
        self._validate_approval(proposal, mode, approval)
        idempotency_key = sha256_json(
            {
                "namespace": self._namespace,
                "fingerprint": proposal.order_fingerprint,
                "action": BrokerAction.PLACE_OPTION_ORDER.value,
            }
        )
        intent = OrderIntent(
            intent_id=uuid5(
                EXECUTION_ID_NAMESPACE,
                f"place:{self._environment.value}:{self._namespace}:{idempotency_key}",
            ),
            created_at=self._clock.now(),
            environment=self._environment,
            namespace=self._namespace,
            proposal_id=proposal.proposal_id,
            risk_decision_id=risk.decision_id,
            approval_id=approval.approval_id if approval else None,
            review_id=review.review_id,
            order_fingerprint=proposal.order_fingerprint,
            idempotency_key=idempotency_key,
            action=BrokerAction.PLACE_OPTION_ORDER,
        )
        capabilities = await self._broker.get_capabilities()
        capability = capabilities.descriptor_for_action(BrokerAction.PLACE_OPTION_ORDER)
        if capability is None or not capabilities.execution_ready:
            raise ExecutionDenied("required place-order capability is unavailable or not ready")
        if self._environment is ExecutionEnvironment.DEMO and capabilities.external_writes_enabled:
            self._safety.emergency_stop("DEMO broker advertised external write authority")
            raise ExecutionDenied("DEMO can never use a broker with external write authority")
        arguments = capability.validate(self._argument_builder(proposal, idempotency_key))
        command = BrokerCommandIntent(
            command_intent_id=uuid5(
                EXECUTION_ID_NAMESPACE,
                f"command:{intent.intent_id}:{capability.schema_hash}",
            ),
            created_at=self._clock.now(),
            order_intent_id=intent.intent_id,
            environment=self._environment,
            namespace=self._namespace,
            action=BrokerAction.PLACE_OPTION_ORDER,
            capability_name=capability.tool_name,
            capability_schema_version=capability.schema_version,
            capability_schema_hash=capability.schema_hash,
            instrument_id=proposal.contract.instrument_id,
            side=proposal.side,
            quantity=proposal.quantity,
            limit_price=proposal.limit_price,
            time_in_force="day",
            validated_arguments=arguments,
            proposal_id=proposal.proposal_id,
            risk_decision_id=risk.decision_id,
            approval_id=approval.approval_id if approval else None,
            quote_snapshot_id=quote.snapshot_id,
            broker_observed_account_snapshot_id=(
                observed_account.snapshot_id
                if observed_account.account_kind is AccountKind.BROKER_OBSERVED
                else None
            ),
            effective_account_snapshot_id=effective_account.snapshot_id,
            policy_version=proposal.policy_version,
            order_fingerprint=proposal.order_fingerprint,
            idempotency_key=idempotency_key,
        )
        await self._store.save_order_intent(intent)
        await self._store.save_command_intent(command)

        pending = BrokerOrder(
            order_id=uuid5(EXECUTION_ID_NAMESPACE, f"pending:{intent.intent_id}"),
            created_at=self._clock.now(),
            intent_id=intent.intent_id,
            environment=self._environment,
            state=OrderState.SUBMITTING,
            contract=proposal.contract,
            side=proposal.side,
            quantity=proposal.quantity,
            limit_price=proposal.limit_price,
            submitted_at=self._clock.now(),
        )
        await self._store.save_order(pending)
        await self._transition(
            intent.intent_id,
            OrderState.INTENT_PERSISTED,
            OrderState.SUBMITTING,
            "durable command ready",
        )

        try:
            current_mode = (
                self._trading_mode() if callable(self._trading_mode) else self._trading_mode
            )
            if current_mode != mode:
                raise ExecutionDenied("trading mode changed before submission")
            self._preflight(proposal, current_mode)
            self._validate_approval(proposal, current_mode, approval)
            if not self._risk.evaluate(proposal, effective_account, quote).allowed:
                raise ExecutionDenied("risk evidence expired before submission")
        except ExecutionDenied:
            await self._store.save_order(pending.model_copy(update={"state": OrderState.REJECTED}))
            await self._transition(
                intent.intent_id,
                OrderState.SUBMITTING,
                OrderState.REJECTED,
                "local pre-submission gate denied; no broker write attempted",
            )
            raise

        try:
            order = await self._broker.place_option_order(command, proposal.contract)
        except asyncio.CancelledError:
            await self._record_interrupted_write(pending, "placement")
            raise
        except (TimeoutError, ConnectionError, SubmissionUnknownError) as exc:
            if self._environment is not ExecutionEnvironment.LIVE:
                raise
            self._safety.emergency_stop("unresolved external broker submission")
            unknown = pending.model_copy(update={"state": OrderState.SUBMISSION_UNKNOWN})
            await self._store.save_order(unknown)
            await self._transition(
                intent.intent_id,
                OrderState.SUBMITTING,
                OrderState.SUBMISSION_UNKNOWN,
                "external broker may have accepted command",
            )
            raise SubmissionUnknownError(
                "broker write outcome is unknown; do not retry before reconciliation"
            ) from exc

        if order.intent_id != intent.intent_id or order.environment is not self._environment:
            if self._environment is ExecutionEnvironment.LIVE:
                self._safety.emergency_stop("broker returned an uncorrelated order")
                raise SubmissionUnknownError(
                    "broker response cannot be correlated to durable intent"
                )
            raise SafetyCriticalError("simulated/shadow broker returned an uncorrelated order")
        await self._store.save_order(order)
        await self._transition(
            intent.intent_id,
            OrderState.SUBMITTING,
            order.state,
            "broker order state observed",
        )
        return ExecutionResult(intent, command, risk, review, order)

    async def execute_entry(
        self,
        proposal: TradeProposal,
        *,
        approval: ExactApproval | None = None,
    ) -> ExecutionResult:
        if proposal.side is not OrderSide.BUY_TO_OPEN:
            raise ExecutionDenied("execute_entry accepts BUY_TO_OPEN only")
        return await self.execute(proposal, approval=approval)

    async def cancel_order(self, target: BrokerOrder) -> CancellationResult:
        async with self._execution_lock:
            return await self._cancel_order(target)

    async def _cancel_order(self, target: BrokerOrder) -> CancellationResult:
        """Persist an exact cancellation command before reaching the broker.

        Cancellation is risk-reducing and does not reuse an entry approval, but
        it is still an external write in LIVE and receives its own fingerprint,
        idempotency key, immutable intent, and unknown-submission handling.
        """

        self._deny_interrupted_writes()
        if target.environment is not self._environment:
            raise ExecutionDenied("target order environment does not match startup binding")
        if target.state not in {OrderState.OPEN, OrderState.PARTIAL}:
            raise ExecutionDenied("only an open or partial order can be canceled")
        if self._kill_switch_active() or not self._healthcheck():
            raise ExecutionDenied("cancellation health/safety preflight failed")
        if not self._safety.permits_risk_reducing_exit():
            raise ExecutionDenied("runtime safety state blocks order cancellation")

        original_intent = await self._store.get_order_intent(target.intent_id)
        original_command = await self._store.get_command_for_order_intent(target.intent_id)
        if original_intent is None or original_command is None:
            raise ExecutionDenied("target order has no complete durable intent evidence")

        effective_account = await self._broker.get_effective_execution_account_state()
        observed_account = await self._broker.get_observed_broker_account_state()
        report = await self._broker.reconcile()
        await self._store.save_reconciliation(report)
        if not report.successful:
            raise ExecutionDenied("broker reconciliation is incomplete")

        target_id = target.broker_order_id or str(target.order_id)
        fingerprint = sha256_json(
            {
                "namespace": self._namespace,
                "action": BrokerAction.CANCEL_OPTION_ORDER.value,
                "target_order_id": target_id,
                "target_intent_id": str(target.intent_id),
            }
        )
        duplicate = await self._store.find_intent_by_fingerprint(fingerprint)
        if duplicate is not None:
            duplicate_order = await self._store.get_order(duplicate.intent_id)
            if duplicate_order and duplicate_order.state is OrderState.SUBMISSION_UNKNOWN:
                raise SubmissionUnknownError(
                    "matching cancellation is unresolved; blind retry is forbidden"
                )
            raise DuplicateOrderError("matching cancellation intent already exists")
        idempotency_key = sha256_json(
            {
                "namespace": self._namespace,
                "fingerprint": fingerprint,
                "action": BrokerAction.CANCEL_OPTION_ORDER.value,
            }
        )
        intent = OrderIntent(
            intent_id=uuid5(
                EXECUTION_ID_NAMESPACE,
                f"cancel:{self._environment.value}:{self._namespace}:{idempotency_key}",
            ),
            created_at=self._clock.now(),
            environment=self._environment,
            namespace=self._namespace,
            proposal_id=original_intent.proposal_id,
            risk_decision_id=original_intent.risk_decision_id,
            approval_id=None,
            review_id=original_intent.review_id,
            order_fingerprint=fingerprint,
            idempotency_key=idempotency_key,
            action=BrokerAction.CANCEL_OPTION_ORDER,
        )
        await self._store.save_order_intent(intent)

        capabilities = await self._broker.get_capabilities()
        capability = capabilities.descriptor_for_action(BrokerAction.CANCEL_OPTION_ORDER)
        if capability is None or not capabilities.cancel_option_orders:
            raise ExecutionDenied("required cancel-order capability is unavailable")
        if self._environment is ExecutionEnvironment.DEMO and capabilities.external_writes_enabled:
            self._safety.emergency_stop("DEMO broker advertised external write authority")
            raise ExecutionDenied("DEMO can never use a broker with external write authority")
        arguments = capability.validate(
            {"order_id": str(target_id), "client_order_id": idempotency_key}
        )
        command = BrokerCommandIntent(
            command_intent_id=uuid5(
                EXECUTION_ID_NAMESPACE,
                f"command:{intent.intent_id}:{capability.schema_hash}",
            ),
            created_at=self._clock.now(),
            order_intent_id=intent.intent_id,
            environment=self._environment,
            namespace=self._namespace,
            action=BrokerAction.CANCEL_OPTION_ORDER,
            capability_name=capability.tool_name,
            capability_schema_version=capability.schema_version,
            capability_schema_hash=capability.schema_hash,
            instrument_id=target.contract.instrument_id,
            side=target.side,
            quantity=max(1, target.quantity - target.filled_quantity),
            limit_price=target.limit_price,
            time_in_force="day",
            validated_arguments=arguments,
            proposal_id=original_command.proposal_id,
            risk_decision_id=original_command.risk_decision_id,
            approval_id=None,
            quote_snapshot_id=original_command.quote_snapshot_id,
            broker_observed_account_snapshot_id=(
                observed_account.snapshot_id
                if observed_account.account_kind is AccountKind.BROKER_OBSERVED
                else None
            ),
            effective_account_snapshot_id=effective_account.snapshot_id,
            policy_version=original_command.policy_version,
            order_fingerprint=fingerprint,
            idempotency_key=idempotency_key,
        )
        await self._store.save_command_intent(command)
        pending = target.model_copy(
            update={"intent_id": intent.intent_id, "state": OrderState.SUBMITTING}
        )
        await self._store.save_order(pending)
        await self._transition(
            intent.intent_id,
            OrderState.INTENT_PERSISTED,
            OrderState.SUBMITTING,
            "durable cancellation command ready",
        )
        if (
            self._kill_switch_active()
            or not self._healthcheck()
            or not self._safety.permits_risk_reducing_exit()
        ):
            await self._store.save_order(pending.model_copy(update={"state": OrderState.REJECTED}))
            await self._transition(
                intent.intent_id,
                OrderState.SUBMITTING,
                OrderState.REJECTED,
                "local cancellation gate denied; no broker write attempted",
            )
            raise ExecutionDenied("cancellation safety changed before transmission")
        try:
            result = await self._broker.cancel_option_order(command, target_id)
        except asyncio.CancelledError:
            await self._record_interrupted_write(pending, "cancellation")
            raise
        except (TimeoutError, ConnectionError, SubmissionUnknownError) as exc:
            if self._environment is not ExecutionEnvironment.LIVE:
                raise
            self._safety.emergency_stop("unresolved external broker cancellation")
            unknown = pending.model_copy(update={"state": OrderState.SUBMISSION_UNKNOWN})
            await self._store.save_order(unknown)
            await self._transition(
                intent.intent_id,
                OrderState.SUBMITTING,
                OrderState.SUBMISSION_UNKNOWN,
                "external broker cancellation outcome is unknown",
            )
            raise SubmissionUnknownError(
                "broker cancellation outcome is unknown; do not retry before reconciliation"
            ) from exc

        correlated = result.model_copy(update={"intent_id": intent.intent_id})
        await self._store.save_order(correlated)
        await self._transition(
            intent.intent_id,
            OrderState.SUBMITTING,
            correlated.state,
            "broker cancellation state observed",
        )
        return CancellationResult(intent, command, correlated)

    async def _record_interrupted_write(self, pending: BrokerOrder, action: str) -> None:
        """Classify cancellation without swallowing it or replaying the command.

        Task cancellation is not proof that the broker failed to accept bytes.
        This also applies to a local shadow-ledger mutation; it never grants a
        Demo adapter external authority. SUBMITTING was durable before the await.
        If this best-effort update fails, that original unresolved evidence and
        the process-local latch remain. No shielded/detached journal task escapes
        controller lifetime tracking. Cancellation-resistant storage may exceed
        this cooperative deadline, so controller shutdown retains its instance
        lock until the executing callback has actually terminated.
        """
        self._interrupted_write_intents.add(pending.intent_id)
        self._safety.emergency_stop("interrupted broker command requires reconciliation")
        try:
            async with asyncio.timeout(self._cancellation_audit_timeout_seconds):
                await self._store.save_order(
                    pending.model_copy(update={"state": OrderState.SUBMISSION_UNKNOWN})
                )
                await self._transition(
                    pending.intent_id,
                    OrderState.SUBMITTING,
                    OrderState.SUBMISSION_UNKNOWN,
                    f"task cancelled during broker {action}; acceptance is unproven",
                )
        except asyncio.CancelledError:
            # A second cancellation must not replace the caller's original one.
            self._safety.emergency_stop("interrupted broker command audit is incomplete")
        except Exception:
            # Never expose upstream connection strings or response bodies.
            self._safety.emergency_stop("interrupted broker command audit is incomplete")

    def _deny_interrupted_writes(self) -> None:
        if self._interrupted_write_intents:
            self._safety.emergency_stop("interrupted broker command requires reconciliation")
            raise ExecutionDenied("interrupted write latch blocks execution until process restart")

    async def reconcile_submission(self, intent_id: UUID) -> SubmissionReconciliation:
        local = await self._store.get_order(intent_id)
        if local is None or local.state is not OrderState.SUBMISSION_UNKNOWN:
            raise ExecutionDenied("intent is not in SUBMISSION_UNKNOWN")
        report = await self._broker.reconcile()
        await self._store.save_reconciliation(report)
        if not report.successful or report.environment is not self._environment:
            return SubmissionReconciliation(intent_id, None, report, False, False)
        orders = await self._broker.get_orders()
        match = next((order for order in orders if order.intent_id == intent_id), None)
        if match is None:
            intent = await self._store.get_order_intent(intent_id)
            # Absence from an open-order list does not establish non-acceptance:
            # an order may already be filled, delayed, or on another page.
            confirmed_keys = report.details.get("negative_match_idempotency_keys", ())
            if (
                intent is None
                or report.details.get("order_history_complete") is not True
                or not isinstance(confirmed_keys, (list, tuple))
                or intent.idempotency_key not in confirmed_keys
            ):
                return SubmissionReconciliation(intent_id, None, report, False, False)
            await self._store.record_negative_reconciliation(intent_id)
            return SubmissionReconciliation(intent_id, None, report, True, True)
        if (
            match.environment is not self._environment
            or match.contract != local.contract
            or match.side != local.side
            or match.quantity != local.quantity
            or match.limit_price != local.limit_price
            or match.state in {OrderState.SUBMITTING, OrderState.SUBMISSION_UNKNOWN}
        ):
            return SubmissionReconciliation(intent_id, None, report, False, False)
        await self._store.save_order(match)
        await self._transition(
            intent_id,
            OrderState.SUBMISSION_UNKNOWN,
            match.state,
            "broker reconciliation matched durable intent",
            reconciliation_evidence=True,
        )
        return SubmissionReconciliation(intent_id, match, report, False, False)

    async def record_broker_update(
        self,
        order: BrokerOrder,
        *,
        fills: tuple[Fill, ...] = (),
        positions: tuple[Position, ...] = (),
        reconciliation_evidence: bool = False,
    ) -> None:
        """Durably apply broker-observed fills/order state/position state.

        Quote consumption belongs to the injected broker, but the resulting
        lifecycle evidence still has to cross the Execution Service boundary so
        an OPEN order cannot silently become FILLED only in broker memory.
        """

        local = await self._store.get_order(order.intent_id)
        if local is None:
            raise ExecutionDenied("broker update has no matching durable local order")
        if order.environment is not self._environment:
            raise ExecutionDenied("broker update environment does not match startup binding")
        if (
            order.order_id != local.order_id
            or order.contract != local.contract
            or order.side is not local.side
            or order.quantity != local.quantity
        ):
            raise ExecutionDenied("broker update does not match the durable exact order")
        if order.filled_quantity < local.filled_quantity:
            raise ExecutionDenied("broker update would decrease filled quantity")
        if any(fill.order_id != order.order_id for fill in fills):
            raise ExecutionDenied("fill does not belong to the updated exact order")
        if any(position.environment is not self._environment for position in positions):
            raise ExecutionDenied("position snapshot environment does not match startup binding")

        changed = order.state is not local.state or order.filled_quantity != local.filled_quantity
        if changed:
            await self._transition(
                order.intent_id,
                local.state,
                order.state,
                "broker fill/order update observed",
                reconciliation_evidence=reconciliation_evidence,
            )
        await self._store.save_order(order)
        for fill in fills:
            await self._store.save_fill(fill)
        await self._store.replace_positions(positions)

    def _preflight(self, proposal: TradeProposal, mode: TradingMode) -> None:
        self._deny_interrupted_writes()
        if self._environment is ExecutionEnvironment.LIVE and mode is TradingMode.SHADOW:
            raise ExecutionDenied("LIVE+SHADOW is not a defined execution mode")
        if mode is TradingMode.RESEARCH:
            raise ExecutionDenied("RESEARCH mode cannot create order intents")
        if self._kill_switch_active():
            raise ExecutionDenied("global kill switch is active")
        if not self._healthcheck():
            raise ExecutionDenied("execution service health check failed")
        if proposal.side is OrderSide.BUY_TO_OPEN and not self._safety.permits_new_entry():
            raise ExecutionDenied("runtime safety state blocks new entries")
        if (
            proposal.side is OrderSide.SELL_TO_CLOSE
            and not self._safety.permits_risk_reducing_exit()
        ):
            raise ExecutionDenied("runtime safety state blocks exits")

    def _validate_binding(self, proposal: TradeProposal) -> None:
        if proposal.environment is not self._environment or proposal.namespace != self._namespace:
            raise ExecutionDenied("proposal environment/namespace does not match startup binding")

    def _validate_approval(
        self,
        proposal: TradeProposal,
        mode: TradingMode,
        approval: ExactApproval | None,
    ) -> None:
        approval_required = (
            mode is TradingMode.APPROVAL
            or mode is TradingMode.EXIT_AUTO
            and proposal.side is OrderSide.BUY_TO_OPEN
        )
        if not approval_required:
            return
        now = self._clock.now()
        if (
            approval is None
            or approval.created_at > now
            or not approval.is_valid_for(proposal, now)
        ):
            raise ExecutionDenied("missing, expired, rejected, or non-exact order approval")

    async def _transition(
        self,
        intent_id: UUID,
        previous: OrderState,
        current: OrderState,
        reason: str,
        *,
        reconciliation_evidence: bool = False,
    ) -> None:
        from app.execution.state_machine import OrderStateMachine

        OrderStateMachine.transition(
            previous,
            current,
            reconciliation_evidence=reconciliation_evidence,
        )
        await self._store.record_transition(
            StateTransitionRecord(
                intent_id=intent_id,
                previous=previous,
                current=current,
                reason=reason,
                recorded_at=self._clock.now(),
                reconciliation_evidence=reconciliation_evidence,
            )
        )

    @staticmethod
    def _default_arguments(proposal: TradeProposal, idempotency_key: str) -> dict[str, Any]:
        return {
            "instrument_id": proposal.contract.instrument_id,
            "side": proposal.side.value,
            "quantity": proposal.quantity,
            "limit_price": str(proposal.limit_price),
            "time_in_force": "day",
            "client_order_id": idempotency_key,
        }
