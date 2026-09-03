from __future__ import annotations

import asyncio
from collections.abc import Mapping
from dataclasses import asdict
from typing import Any, Protocol, TypeVar
from uuid import UUID

from pydantic import BaseModel
from sqlalchemy.exc import IntegrityError

from app.broker.base import ReconciliationReport
from app.clock.base import Clock
from app.config import RuntimeBinding
from app.db.models import EXECUTION_UNIQUE_KEYS
from app.domain.enums import OrderState
from app.domain.models import (
    BrokerCommandIntent,
    BrokerOrder,
    BrokerReview,
    ExactApproval,
    Fill,
    OrderIntent,
    Position,
    RiskDecision,
    TradeProposal,
    sha256_json,
)
from app.exceptions import SafetyCriticalError
from app.execution.service import DuplicateOrderError, StateTransitionRecord
from app.execution.state_machine import OrderStateMachine

ModelT = TypeVar("ModelT", bound=BaseModel)


class ExecutionAuditRepository(Protocol):
    binding: RuntimeBinding

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


class PostgresExecutionStore:
    """Durable execution journal over a DatabaseManager-bound audit repository.

    Immutable records are retry-idempotent and defended by PostgreSQL unique
    indexes, not only the local lock. Orders and position snapshots are append
    only. Reads use ingestion sequence rather than replay/business timestamps.
    Runtime scans fail closed at their bound instead of truncating evidence.
    """

    def __init__(self, repository: ExecutionAuditRepository, clock: Clock) -> None:
        if not repository.binding.idempotency_namespace:
            raise ValueError("execution namespace is required")
        self.repository = repository
        self.binding = repository.binding
        self._clock = clock
        self._lock = asyncio.Lock()

    def _guard(self, value: BaseModel | Mapping[str, Any]) -> None:
        payload = value.model_dump(mode="json") if isinstance(value, BaseModel) else value
        environment = payload.get("environment", self.binding.environment.value)
        if getattr(environment, "value", environment) != self.binding.environment.value:
            raise SafetyCriticalError("cross-environment execution persistence attempt")
        if payload.get("namespace", self.binding.idempotency_namespace) != (
            self.binding.idempotency_namespace
        ):
            raise SafetyCriticalError("cross-namespace execution persistence attempt")

    def _payload(self, row: dict[str, Any]) -> dict[str, Any]:
        if (
            row.get("environment") != self.binding.environment.value
            or row.get("namespace") != self.binding.idempotency_namespace
        ):
            raise SafetyCriticalError("execution read crossed its environment/namespace binding")
        payload: dict[str, Any] = row["payload"]
        self._guard(payload)
        return payload

    async def _get(self, table: str, key: str, value: Any, model: type[ModelT]) -> ModelT | None:
        row = await self.repository.find_payload(table, key, str(value))
        if row is None:
            return None
        result = model.model_validate(self._payload(row))
        self._guard(result)
        return result

    async def _save_immutable(
        self, table: str, value: BaseModel | Mapping[str, Any], id_key: str
    ) -> None:
        self._guard(value)
        payload = value.model_dump(mode="json") if isinstance(value, BaseModel) else dict(value)
        keys = EXECUTION_UNIQUE_KEYS.get(table, (id_key,))

        async def existing() -> bool:
            matched = False
            for key in keys:
                if key not in payload:
                    continue
                row = await self.repository.find_payload(table, key, payload[key])
                if row is None:
                    continue
                other = self._payload(row)
                if sha256_json(other) != sha256_json(payload):
                    raise DuplicateOrderError(f"{table} {key} reused with different content")
                matched = True
            return matched

        async with self._lock:
            if await existing():
                return
            try:
                await self.repository.append(table, value)
            except IntegrityError as exc:
                # A competing process may have committed after our lookup.
                # The unique index decides the winner; only identical content
                # is a successful retry. Unrelated integrity failures are fatal.
                if await existing():
                    return
                raise SafetyCriticalError(f"durable {table} insert failed") from exc

    async def save_proposal(self, proposal: TradeProposal) -> None:
        await self._save_immutable("trade_proposals", proposal, "proposal_id")

    async def get_proposal(self, proposal_id: UUID) -> TradeProposal | None:
        return await self._get("trade_proposals", "proposal_id", proposal_id, TradeProposal)

    async def save_risk_decision(self, decision: RiskDecision) -> None:
        await self._save_immutable("risk_decisions", decision, "decision_id")

    async def get_risk_decision(self, decision_id: UUID) -> RiskDecision | None:
        return await self._get("risk_decisions", "decision_id", decision_id, RiskDecision)

    async def save_approval(self, approval: ExactApproval) -> None:
        await self._save_immutable("approvals", approval, "approval_id")

    async def get_approval(self, approval_id: UUID) -> ExactApproval | None:
        return await self._get("approvals", "approval_id", approval_id, ExactApproval)

    async def save_review(self, review: BrokerReview) -> None:
        await self._save_immutable("broker_reviews", review, "review_id")

    async def get_review(self, review_id: UUID) -> BrokerReview | None:
        return await self._get("broker_reviews", "review_id", review_id, BrokerReview)

    async def save_order_intent(self, intent: OrderIntent) -> None:
        await self._save_immutable("order_intents", intent, "intent_id")

    async def get_order_intent(self, intent_id: UUID) -> OrderIntent | None:
        return await self._get("order_intents", "intent_id", intent_id, OrderIntent)

    async def find_intent_by_fingerprint(self, fingerprint: str) -> OrderIntent | None:
        return await self._get("order_intents", "order_fingerprint", fingerprint, OrderIntent)

    async def save_command_intent(self, command: BrokerCommandIntent) -> None:
        self._guard(command)
        intent = await self.get_order_intent(command.order_intent_id)
        if intent is None:
            raise SafetyCriticalError("command cannot precede its durable order intent")
        fields = (
            "environment",
            "namespace",
            "proposal_id",
            "risk_decision_id",
            "approval_id",
            "order_fingerprint",
            "idempotency_key",
            "action",
        )
        if any(getattr(command, key) != getattr(intent, key) for key in fields):
            raise SafetyCriticalError("command does not match its durable exact order intent")
        await self._save_immutable("broker_command_intents", command, "command_intent_id")

    async def get_command_intent(self, command_intent_id: UUID) -> BrokerCommandIntent | None:
        return await self._get(
            "broker_command_intents", "command_intent_id", command_intent_id, BrokerCommandIntent
        )

    async def get_command_for_order_intent(self, intent_id: UUID) -> BrokerCommandIntent | None:
        return await self._get(
            "broker_command_intents", "order_intent_id", intent_id, BrokerCommandIntent
        )

    async def save_order(self, order: BrokerOrder) -> None:
        self._guard(order)
        if await self.get_order_intent(order.intent_id) is None:
            raise SafetyCriticalError("broker order cannot precede its durable order intent")
        async with self._lock:
            previous = await self.get_order(order.intent_id)
            if previous == order:
                return
            if previous is not None:
                if (
                    previous.contract != order.contract
                    or previous.side != order.side
                    or previous.quantity != order.quantity
                    or previous.limit_price != order.limit_price
                    or order.filled_quantity < previous.filled_quantity
                ):
                    raise SafetyCriticalError("broker order changed its durable exact terms")
                if previous.order_id != order.order_id and previous.state not in {
                    OrderState.SUBMITTING,
                    OrderState.SUBMISSION_UNKNOWN,
                }:
                    raise SafetyCriticalError("broker order identity changed after submission")
            await self.repository.append("orders", order)

    async def get_order(self, intent_id: UUID) -> BrokerOrder | None:
        return await self._get("orders", "intent_id", intent_id, BrokerOrder)

    async def save_fill(self, fill: Fill) -> None:
        order = await self._get("orders", "order_id", fill.order_id, BrokerOrder)
        if order is None:
            raise SafetyCriticalError("fill cannot precede its durable broker order")
        await self._save_immutable("fills", fill, "fill_id")

    async def get_fill(self, fill_id: UUID) -> Fill | None:
        return await self._get("fills", "fill_id", fill_id, Fill)

    async def replace_positions(self, positions: tuple[Position, ...]) -> None:
        for position in positions:
            self._guard(position)
        if len({position.position_id for position in positions}) != len(positions):
            raise SafetyCriticalError("position snapshot repeats an identity")
        ordered = tuple(sorted(positions, key=lambda position: str(position.position_id)))
        async with self._lock:
            if await self.list_positions() == ordered:
                # An empty first snapshot still needs a durable checkpoint.
                existing = await self.repository.find_payload(
                    "position_snapshots", "record_kind", "execution_positions"
                )
                if existing is not None:
                    return
            for position in ordered:
                await self.repository.append("positions", position)
            await self.repository.append(
                "position_snapshots",
                {
                    **self._event("execution_positions"),
                    "positions": [position.model_dump(mode="json") for position in ordered],
                },
            )

    async def list_positions(self) -> tuple[Position, ...]:
        row = await self.repository.find_payload(
            "position_snapshots", "record_kind", "execution_positions"
        )
        if row is None:
            return ()
        positions = tuple(Position.model_validate(item) for item in self._payload(row)["positions"])
        for position in positions:
            self._guard(position)
        return positions

    def _event(self, record_kind: str) -> dict[str, Any]:
        return {
            "created_at": self._clock.now().isoformat().replace("+00:00", "Z"),
            "environment": self.binding.environment.value,
            "namespace": self.binding.idempotency_namespace,
            "record_kind": record_kind,
        }

    async def save_reconciliation(self, report: ReconciliationReport) -> None:
        self._guard(report)
        self._guard(report.observed_account)
        self._guard(report.effective_account)
        data = report.model_dump(mode="json")
        event = {
            **self._event("execution_reconciliation"),
            "created_at": data["reconciled_at"],
            "reconciliation_id": sha256_json(data),
            "report": data,
        }
        await self._save_immutable("reconciliation_events", event, "reconciliation_id")

    async def record_transition(self, record: StateTransitionRecord) -> None:
        if await self.get_order_intent(record.intent_id) is None:
            raise SafetyCriticalError("transition cannot precede its durable order intent")
        OrderStateMachine.transition(
            record.previous,
            record.current,
            reconciliation_evidence=record.reconciliation_evidence,
        )
        if record.recorded_at.tzinfo is None or record.recorded_at.utcoffset() is None:
            raise ValueError("transition timestamp must be timezone-aware")
        data = asdict(record)
        event = {
            **self._event("execution_transition"),
            "created_at": record.recorded_at.isoformat().replace("+00:00", "Z"),
            "transition_id": sha256_json(data),
            "intent_id": str(record.intent_id),
            "previous": record.previous.value,
            "current": record.current.value,
            "reason": record.reason,
            "reconciliation_evidence": record.reconciliation_evidence,
        }
        await self._save_immutable("environment_audit_events", event, "transition_id")

    async def record_negative_reconciliation(self, intent_id: UUID) -> None:
        if await self.get_order_intent(intent_id) is None:
            raise SafetyCriticalError("negative reconciliation requires a durable intent")
        existing = await self.repository.find_payload(
            "reconciliation_events", "negative_reconciliation_id", str(intent_id)
        )
        if existing is not None:
            self._payload(existing)
            return
        await self._save_immutable(
            "reconciliation_events",
            {
                **self._event("negative_reconciliation"),
                "negative_reconciliation_id": str(intent_id),
                "intent_id": str(intent_id),
            },
            "negative_reconciliation_id",
        )

    async def has_negative_reconciliation(self, intent_id: UUID) -> bool:
        row = await self.repository.find_payload(
            "reconciliation_events", "negative_reconciliation_id", str(intent_id)
        )
        if row is None:
            return False
        self._payload(row)
        return True

    async def _scan(
        self,
        table: str,
        *,
        filters: Mapping[str, Any] | None = None,
        max_records: int = 100_000,
    ) -> list[dict[str, Any]]:
        if max_records < 1:
            raise ValueError("max_records must be positive")
        records: list[dict[str, Any]] = []
        cursor: int | None = None
        while True:
            page = await self.repository.list_payloads(
                table,
                filters=filters,
                limit=min(1000, max_records + 1 - len(records)),
                before_sequence=cursor,
            )
            if not page:
                return records
            for row in page:
                self._payload(row)
                sequence = row.get("append_sequence")
                if not isinstance(sequence, int) or (cursor is not None and sequence >= cursor):
                    raise SafetyCriticalError("execution audit pagination is not strictly ordered")
                cursor = sequence
                records.append(row)
                if len(records) > max_records:
                    raise SafetyCriticalError(
                        "execution audit scan bound exceeded; reconciliation required"
                    )

    async def list_latest_orders(self, *, max_records: int = 100_000) -> tuple[BrokerOrder, ...]:
        seen_intents: set[UUID] = set()
        seen_orders: set[UUID] = set()
        result: list[BrokerOrder] = []
        for row in await self._scan("orders", max_records=max_records):
            order = BrokerOrder.model_validate(self._payload(row))
            if order.intent_id in seen_intents:
                continue
            seen_intents.add(order.intent_id)
            if order.order_id not in seen_orders:
                seen_orders.add(order.order_id)
                result.append(order)
        return tuple(result)

    async def unresolved_intents(self, *, max_records: int = 100_000) -> tuple[OrderIntent, ...]:
        result: list[OrderIntent] = []
        for row in await self._scan("order_intents", max_records=max_records):
            intent = OrderIntent.model_validate(self._payload(row))
            order = await self.get_order(intent.intent_id)
            command = await self.get_command_for_order_intent(intent.intent_id)
            if (
                command is None
                or order is None
                or order.state
                in {
                    OrderState.SUBMITTING,
                    OrderState.SUBMISSION_UNKNOWN,
                }
            ):
                result.append(intent)
        return tuple(result)

    async def list_fills(self, order_id: UUID | None = None) -> tuple[Fill, ...]:
        filters = {"order_id": str(order_id)} if order_id else None
        return tuple(
            Fill.model_validate(self._payload(row))
            for row in await self._scan("fills", filters=filters)
        )

    async def list_transitions(self, intent_id: UUID) -> tuple[StateTransitionRecord, ...]:
        rows = await self._scan("environment_audit_events", filters={"intent_id": str(intent_id)})
        from datetime import datetime

        return tuple(
            StateTransitionRecord(
                intent_id=intent_id,
                previous=OrderState(row["payload"]["previous"]),
                current=OrderState(row["payload"]["current"]),
                reason=row["payload"]["reason"],
                recorded_at=datetime.fromisoformat(
                    row["payload"]["created_at"].replace("Z", "+00:00")
                ),
                reconciliation_evidence=row["payload"]["reconciliation_evidence"],
            )
            for row in rows
            if row["payload"].get("record_kind") == "execution_transition"
        )

    async def list_reconciliations(self) -> tuple[ReconciliationReport, ...]:
        rows = await self._scan(
            "reconciliation_events", filters={"record_kind": "execution_reconciliation"}
        )
        return tuple(ReconciliationReport.model_validate(row["payload"]["report"]) for row in rows)
