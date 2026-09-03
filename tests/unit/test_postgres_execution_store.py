from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any
from uuid import UUID, uuid4

import pytest
from pydantic import BaseModel
from sqlalchemy.exc import IntegrityError

from app.broker.base import ReconciliationReport
from app.clock.base import VirtualClock
from app.config import RuntimeBinding
from app.db.repository import InMemoryAuditRepository
from app.domain.enums import AccountKind, BrokerAction, ExecutionEnvironment, OrderState
from app.domain.models import (
    AccountSnapshot,
    BrokerCommandIntent,
    BrokerOrder,
    BrokerReview,
    ExactApproval,
    Fill,
    OrderIntent,
    Position,
    RiskDecision,
    TradeProposal,
)
from app.exceptions import SafetyCriticalError
from app.execution.postgres_store import PostgresExecutionStore
from app.execution.service import DuplicateOrderError, ExecutionStore, StateTransitionRecord


@dataclass(frozen=True)
class Evidence:
    proposal: TradeProposal
    risk: RiskDecision
    approval: ExactApproval
    review: BrokerReview
    intent: OrderIntent
    command: BrokerCommandIntent
    order: BrokerOrder
    fill: Fill
    position: Position
    reconciliation: ReconciliationReport


@pytest.fixture
def evidence(proposal: TradeProposal, instant: datetime) -> Evidence:
    account = AccountSnapshot(
        created_at=instant,
        environment=proposal.environment,
        account_kind=AccountKind.SIMULATED,
        cash=Decimal("300"),
        buying_power=Decimal("300"),
        as_of=instant,
        is_authenticated=False,
        state_known=True,
    )
    risk = RiskDecision(
        created_at=instant,
        environment=proposal.environment,
        proposal_id=proposal.proposal_id,
        allowed=True,
        failed_rules=(),
        passed_rules=("fixture",),
        account_snapshot_id=account.snapshot_id,
        proposed_max_loss=proposal.max_loss,
        resulting_aggregate_risk=proposal.max_loss,
        data_fresh=True,
        risk_config_version="risk-v1",
    )
    approval = ExactApproval(
        created_at=instant,
        environment=proposal.environment,
        namespace=proposal.namespace,
        proposal_id=proposal.proposal_id,
        order_fingerprint=proposal.order_fingerprint,
        maximum_limit_price=proposal.limit_price,
        expires_at=instant + timedelta(minutes=5),
        approved_by="tester",
    )
    review = BrokerReview(
        created_at=instant,
        environment=proposal.environment,
        proposal_id=proposal.proposal_id,
        accepted=True,
    )
    intent = OrderIntent(
        created_at=instant,
        environment=proposal.environment,
        namespace=proposal.namespace,
        proposal_id=proposal.proposal_id,
        risk_decision_id=risk.decision_id,
        approval_id=approval.approval_id,
        review_id=review.review_id,
        order_fingerprint=proposal.order_fingerprint,
        idempotency_key="entry-key",
        action=BrokerAction.PLACE_OPTION_ORDER,
    )
    command = BrokerCommandIntent(
        created_at=instant,
        order_intent_id=intent.intent_id,
        environment=proposal.environment,
        namespace=proposal.namespace,
        action=intent.action,
        capability_name="place_option_order",
        capability_schema_version="v1",
        capability_schema_hash="schema-hash",
        instrument_id=proposal.contract.instrument_id,
        side=proposal.side,
        quantity=proposal.quantity,
        limit_price=proposal.limit_price,
        validated_arguments={},
        proposal_id=proposal.proposal_id,
        risk_decision_id=risk.decision_id,
        approval_id=approval.approval_id,
        quote_snapshot_id=proposal.quote_snapshot_id,
        broker_observed_account_snapshot_id=None,
        effective_account_snapshot_id=account.snapshot_id,
        policy_version=proposal.policy_version,
        order_fingerprint=proposal.order_fingerprint,
        idempotency_key=intent.idempotency_key,
    )
    order = BrokerOrder(
        created_at=instant,
        intent_id=intent.intent_id,
        environment=proposal.environment,
        state=OrderState.OPEN,
        contract=proposal.contract,
        side=proposal.side,
        quantity=proposal.quantity,
        limit_price=proposal.limit_price,
    )
    fill = Fill(
        created_at=instant,
        order_id=order.order_id,
        quantity=1,
        price=proposal.limit_price,
        market_event_ids=("quote-1",),
        fill_model_version="v1",
        deterministic_seed=1,
        reason="fixture fill",
    )
    position = Position(
        created_at=instant,
        environment=proposal.environment,
        contract=proposal.contract,
        quantity=1,
        average_entry_price=proposal.limit_price,
        current_bid=Decimal("0.07"),
        current_ask=proposal.limit_price,
        thesis_id=proposal.proposal_id,
        invalidation_conditions=proposal.invalidation_conditions,
        exit_policy_version="exit-v1",
    )
    reconciliation = ReconciliationReport(
        environment=proposal.environment,
        reconciled_at=instant,
        successful=True,
        observed_account=account,
        effective_account=account,
        position_count=0,
        open_order_count=1,
    )
    return Evidence(
        proposal, risk, approval, review, intent, command, order, fill, position, reconciliation
    )


async def seed(store: PostgresExecutionStore, evidence: Evidence) -> None:
    await store.save_proposal(evidence.proposal)
    await store.save_risk_decision(evidence.risk)
    await store.save_approval(evidence.approval)
    await store.save_review(evidence.review)
    await store.save_order_intent(evidence.intent)
    await store.save_command_intent(evidence.command)
    await store.save_order(evidence.order)


@pytest.mark.asyncio
async def test_execution_store_round_trip_survives_store_reconstruction(
    demo_binding: RuntimeBinding,
    clock: VirtualClock,
    evidence: Evidence,
) -> None:
    repository = InMemoryAuditRepository(demo_binding)
    store = PostgresExecutionStore(repository, clock)
    contract: ExecutionStore = store
    await seed(store, evidence)
    await contract.save_fill(evidence.fill)
    await contract.replace_positions((evidence.position,))
    await contract.save_reconciliation(evidence.reconciliation)
    transition = StateTransitionRecord(
        evidence.intent.intent_id,
        OrderState.INTENT_PERSISTED,
        OrderState.SUBMITTING,
        "durable command ready",
        clock.now(),
    )
    await contract.record_transition(transition)
    await contract.record_negative_reconciliation(evidence.intent.intent_id)

    restored = PostgresExecutionStore(repository, clock)
    assert await restored.get_proposal(evidence.proposal.proposal_id) == evidence.proposal
    assert await restored.get_risk_decision(evidence.risk.decision_id) == evidence.risk
    assert await restored.get_approval(evidence.approval.approval_id) == evidence.approval
    assert await restored.get_review(evidence.review.review_id) == evidence.review
    assert await restored.get_order_intent(evidence.intent.intent_id) == evidence.intent
    assert (
        await restored.find_intent_by_fingerprint(evidence.intent.order_fingerprint)
        == evidence.intent
    )
    assert await restored.get_command_intent(evidence.command.command_intent_id) == evidence.command
    assert (
        await restored.get_command_for_order_intent(evidence.intent.intent_id) == evidence.command
    )
    assert await restored.get_order(evidence.intent.intent_id) == evidence.order
    assert await restored.get_fill(evidence.fill.fill_id) == evidence.fill
    assert await restored.list_fills(evidence.order.order_id) == (evidence.fill,)
    assert await restored.list_positions() == (evidence.position,)
    assert await restored.list_latest_orders() == (evidence.order,)
    assert await restored.list_reconciliations() == (evidence.reconciliation,)
    assert await restored.list_transitions(evidence.intent.intent_id) == (transition,)
    assert await restored.has_negative_reconciliation(evidence.intent.intent_id)
    assert await restored.unresolved_intents() == ()


@pytest.mark.asyncio
async def test_identical_immutable_retries_do_not_duplicate_rows(
    demo_binding: RuntimeBinding,
    clock: VirtualClock,
    evidence: Evidence,
) -> None:
    repository = InMemoryAuditRepository(demo_binding)
    store = PostgresExecutionStore(repository, clock)
    await seed(store, evidence)
    await seed(store, evidence)
    await store.save_fill(evidence.fill)
    await store.save_fill(evidence.fill)
    await store.save_reconciliation(evidence.reconciliation)
    await store.save_reconciliation(evidence.reconciliation)
    await store.replace_positions((evidence.position,))
    await store.replace_positions((evidence.position,))
    for table in (
        "trade_proposals",
        "risk_decisions",
        "approvals",
        "broker_reviews",
        "order_intents",
        "broker_command_intents",
        "orders",
        "fills",
        "reconciliation_events",
        "position_snapshots",
    ):
        assert len(await repository.list(table)) == 1


@pytest.mark.parametrize("collision_key", ["intent_id", "order_fingerprint", "idempotency_key"])
@pytest.mark.asyncio
async def test_intent_identity_collision_is_rejected(
    demo_binding: RuntimeBinding,
    clock: VirtualClock,
    evidence: Evidence,
    collision_key: str,
) -> None:
    store = PostgresExecutionStore(InMemoryAuditRepository(demo_binding), clock)
    await store.save_order_intent(evidence.intent)
    changes = {"intent_id": uuid4(), "order_fingerprint": "other", "idempotency_key": "other"}
    changes[collision_key] = getattr(evidence.intent, collision_key)
    with pytest.raises(DuplicateOrderError, match="reused with different content"):
        await store.save_order_intent(evidence.intent.model_copy(update=changes))


@pytest.mark.asyncio
async def test_changed_content_under_same_immutable_id_is_rejected(
    demo_binding: RuntimeBinding,
    clock: VirtualClock,
    evidence: Evidence,
) -> None:
    store = PostgresExecutionStore(InMemoryAuditRepository(demo_binding), clock)
    await seed(store, evidence)
    await store.save_fill(evidence.fill)
    with pytest.raises(DuplicateOrderError):
        await store.save_proposal(evidence.proposal.model_copy(update={"thesis": "changed"}))
    with pytest.raises(DuplicateOrderError):
        await store.save_command_intent(evidence.command.model_copy(update={"quantity": 2}))
    with pytest.raises(DuplicateOrderError):
        await store.save_fill(evidence.fill.model_copy(update={"price": Decimal("0.09")}))


@pytest.mark.parametrize(
    "field,value", [("environment", ExecutionEnvironment.LIVE), ("namespace", "other-namespace")]
)
@pytest.mark.asyncio
async def test_binding_guards_apply_before_persistence(
    demo_binding: RuntimeBinding,
    clock: VirtualClock,
    evidence: Evidence,
    field: str,
    value: object,
) -> None:
    repository = InMemoryAuditRepository(demo_binding)
    store = PostgresExecutionStore(repository, clock)
    with pytest.raises(SafetyCriticalError, match="cross-"):
        await store.save_order_intent(evidence.intent.model_copy(update={field: value}))
    assert await repository.list("order_intents") == []


@pytest.mark.asyncio
async def test_dependencies_and_exact_command_correlation_are_required(
    demo_binding: RuntimeBinding,
    clock: VirtualClock,
    evidence: Evidence,
) -> None:
    store = PostgresExecutionStore(InMemoryAuditRepository(demo_binding), clock)
    with pytest.raises(SafetyCriticalError, match="command cannot precede"):
        await store.save_command_intent(evidence.command)
    with pytest.raises(SafetyCriticalError, match="order cannot precede"):
        await store.save_order(evidence.order)
    with pytest.raises(SafetyCriticalError, match="fill cannot precede"):
        await store.save_fill(evidence.fill)
    with pytest.raises(SafetyCriticalError, match="requires a durable intent"):
        await store.record_negative_reconciliation(evidence.intent.intent_id)
    await store.save_order_intent(evidence.intent)
    with pytest.raises(SafetyCriticalError, match="does not match"):
        await store.save_command_intent(
            evidence.command.model_copy(update={"idempotency_key": "bad"})
        )


@pytest.mark.asyncio
async def test_same_or_older_business_timestamps_do_not_hide_new_order_state(
    demo_binding: RuntimeBinding,
    clock: VirtualClock,
    evidence: Evidence,
) -> None:
    store = PostgresExecutionStore(InMemoryAuditRepository(demo_binding), clock)
    await seed(store, evidence)
    filled = evidence.order.model_copy(
        update={
            "created_at": evidence.order.created_at - timedelta(days=1),
            "state": OrderState.FILLED,
            "filled_quantity": 1,
        }
    )
    await store.save_order(filled)
    assert await store.get_order(evidence.intent.intent_id) == filled
    assert await store.list_latest_orders() == (filled,)
    with pytest.raises(SafetyCriticalError, match="exact terms"):
        await store.save_order(evidence.order)


@pytest.mark.asyncio
async def test_cancellation_supersedes_the_original_physical_order_in_startup_view(
    demo_binding: RuntimeBinding,
    clock: VirtualClock,
    evidence: Evidence,
) -> None:
    store = PostgresExecutionStore(InMemoryAuditRepository(demo_binding), clock)
    await seed(store, evidence)
    cancel_intent = evidence.intent.model_copy(
        update={
            "intent_id": uuid4(),
            "order_fingerprint": "cancel-fingerprint",
            "idempotency_key": "cancel-key",
            "action": BrokerAction.CANCEL_OPTION_ORDER,
        }
    )
    await store.save_order_intent(cancel_intent)
    canceled = evidence.order.model_copy(
        update={
            "intent_id": cancel_intent.intent_id,
            "state": OrderState.CANCELED,
        }
    )
    await store.save_order(canceled)
    assert await store.list_latest_orders() == (canceled,)


@pytest.mark.asyncio
async def test_unresolved_intents_cover_the_preorder_crash_gap(
    demo_binding: RuntimeBinding,
    clock: VirtualClock,
    evidence: Evidence,
) -> None:
    store = PostgresExecutionStore(InMemoryAuditRepository(demo_binding), clock)
    await store.save_order_intent(evidence.intent)
    assert await store.unresolved_intents() == (evidence.intent,)
    await store.save_command_intent(evidence.command)
    assert await store.unresolved_intents() == (evidence.intent,)
    await store.save_order(
        evidence.order.model_copy(update={"state": OrderState.SUBMISSION_UNKNOWN})
    )
    assert await store.unresolved_intents() == (evidence.intent,)
    await store.save_order(evidence.order)
    assert await store.unresolved_intents() == ()


@pytest.mark.asyncio
async def test_runtime_scan_fails_closed_instead_of_silently_truncating(
    demo_binding: RuntimeBinding,
    clock: VirtualClock,
    evidence: Evidence,
) -> None:
    store = PostgresExecutionStore(InMemoryAuditRepository(demo_binding), clock)
    await seed(store, evidence)
    await store.save_order(evidence.order.model_copy(update={"state": OrderState.CANCELED}))
    with pytest.raises(SafetyCriticalError, match="scan bound exceeded"):
        await store.list_latest_orders(max_records=1)


@pytest.mark.asyncio
async def test_empty_position_snapshot_durably_replaces_previous_positions(
    demo_binding: RuntimeBinding,
    clock: VirtualClock,
    evidence: Evidence,
) -> None:
    repository = InMemoryAuditRepository(demo_binding)
    store = PostgresExecutionStore(repository, clock)
    await store.replace_positions((evidence.position,))
    await store.replace_positions(())
    restored = PostgresExecutionStore(repository, clock)
    assert await restored.list_positions() == ()
    assert len(await repository.list("position_snapshots")) == 2
    with pytest.raises(SafetyCriticalError, match="cross-environment"):
        await store.replace_positions(
            (evidence.position.model_copy(update={"environment": ExecutionEnvironment.LIVE}),)
        )


class RacingRepository(InMemoryAuditRepository):
    def __init__(self, binding: RuntimeBinding, *, conflict: bool) -> None:
        super().__init__(binding)
        self.conflict = conflict

    async def append(self, table: str, value: BaseModel | Mapping[str, Any]) -> UUID:
        if table == "order_intents":
            assert isinstance(value, OrderIntent)
            competing = (
                value.model_copy(update={"state": OrderState.REVIEWED}) if self.conflict else value
            )
            await super().append(table, competing)
            raise IntegrityError("unique index conflict", {}, RuntimeError("competing writer"))
        return await super().append(table, value)


@pytest.mark.parametrize("conflict", [False, True])
@pytest.mark.asyncio
async def test_database_unique_race_is_only_idempotent_for_identical_content(
    demo_binding: RuntimeBinding,
    clock: VirtualClock,
    evidence: Evidence,
    conflict: bool,
) -> None:
    repository = RacingRepository(demo_binding, conflict=conflict)
    store = PostgresExecutionStore(repository, clock)
    if conflict:
        with pytest.raises(DuplicateOrderError, match="different content"):
            await store.save_order_intent(evidence.intent)
    else:
        await store.save_order_intent(evidence.intent)
    assert len(await repository.list("order_intents")) == 1


@pytest.mark.asyncio
async def test_database_failure_is_not_cached_as_success(
    demo_binding: RuntimeBinding,
    clock: VirtualClock,
    evidence: Evidence,
) -> None:
    repository = InMemoryAuditRepository(demo_binding)
    store = PostgresExecutionStore(repository, clock)
    repository.writable = False
    with pytest.raises(SafetyCriticalError, match="not writable"):
        await store.save_order_intent(evidence.intent)
    repository.writable = True
    await store.save_order_intent(evidence.intent)
    assert await store.get_order_intent(evidence.intent.intent_id) == evidence.intent
