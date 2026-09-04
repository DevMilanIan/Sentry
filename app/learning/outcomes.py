"""Bounded, deterministic reviews of durable long-option flat-to-flat cycles.

This is journal arithmetic, not a strategy evaluator or an order producer. It
does not infer commissions, intratrade extrema, catalysts, or investment skill.
"""

from __future__ import annotations

import asyncio
import math
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import ROUND_HALF_EVEN, Decimal, localcontext
from typing import Any, Literal
from uuid import UUID

from pydantic import Field, ValidationError, field_validator
from sqlalchemy.exc import IntegrityError

from app.clock.base import Clock
from app.domain.enums import ExecutionEnvironment, OrderSide
from app.domain.models import BrokerOrder, Fill, OptionContract, TimestampedModel, sha256_json
from app.exceptions import DataInvalidError, SafetyCriticalError
from app.execution.postgres_store import ExecutionAuditRepository

OUTCOME_KIND: Literal["closed_position_review"] = "closed_position_review"
OUTCOME_VERSION: Literal["closed-position-review-v1"] = "closed-position-review-v1"


class ClosedTradeOutcome(TimestampedModel):
    record_kind: Literal["closed_position_review"] = OUTCOME_KIND
    version: Literal["closed-position-review-v1"] = OUTCOME_VERSION
    outcome_id: str = Field(pattern=r"^[a-f0-9]{64}$")
    cycle_id: str = Field(pattern=r"^[a-f0-9]{64}$")
    environment: ExecutionEnvironment
    namespace: str = Field(min_length=1)
    contract: OptionContract
    opened_at: datetime
    closed_at: datetime
    reviewed_at: datetime
    entry_fill_ids: tuple[UUID, ...]
    exit_fill_ids: tuple[UUID, ...]
    order_ids: tuple[UUID, ...]
    contracts_opened: int = Field(gt=0)
    contracts_closed: int = Field(gt=0)
    gross_entry_cost: Decimal = Field(gt=0)
    gross_exit_proceeds: Decimal = Field(gt=0)
    gross_realized_pnl: Decimal
    gross_return_percent: Decimal
    hold_seconds: Decimal = Field(ge=0)
    source_evidence_hash: str = Field(pattern=r"^[a-f0-9]{64}$")
    ordering_basis: Literal["fill_time_then_append_sequence"] = "fill_time_then_append_sequence"
    hold_time_basis: Literal["first_entry_to_final_exit"] = "first_entry_to_final_exit"
    fees: None = None
    net_realized_pnl: None = None
    maximum_adverse_excursion: None = None
    maximum_favorable_excursion: None = None
    catalyst_assessment: None = None
    configuration_changes_applied: Literal[False] = False
    limitations: tuple[str, ...] = (
        "Gross fill arithmetic only; fees and net P&L are unknown.",
        "No quote-path evidence: MAE, MFE, spread, theta, and IV attribution are unknown.",
        "No thesis/catalyst or strategy-quality inference; no configuration is applied.",
        "An unclosed position produces no completed outcome.",
    )

    @field_validator("opened_at", "closed_at", "reviewed_at")
    @classmethod
    def aware_timestamp(cls, value: datetime) -> datetime:
        return _aware(value)


def _aware(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise DataInvalidError("review evidence requires timezone-aware timestamps")
    return value.astimezone(UTC)


def _timestamp(value: Any) -> datetime:
    if isinstance(value, str):
        value = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if not isinstance(value, datetime):
        raise DataInvalidError("review evidence timestamp is missing or invalid")
    return _aware(value)


def _order_terms(order: BrokerOrder) -> dict[str, Any]:
    # Append-only order state snapshots may change filled_quantity/state, not
    # these exact terms. Hash only the terms actually used by this arithmetic.
    return order.model_dump(
        mode="json",
        include={
            "order_id",
            "intent_id",
            "environment",
            "contract",
            "side",
            "quantity",
            "limit_price",
        },
    )


@dataclass
class _Cycle:
    contract: OptionContract
    fills: list[Fill] = field(default_factory=list)
    entries: list[Fill] = field(default_factory=list)
    exits: list[Fill] = field(default_factory=list)
    balance: int = 0


class ClosedPositionReviewWorker:
    """One serialized tick; reject incomplete scans instead of truncating evidence.

    `tick` returns only newly appended outcomes. Outcomes are immutable and
    retry-safe by stable outcome_id, including an ambiguous post-commit failure.
    Multiple processes require the database's unique outcome_id index; the
    per-instance lock alone does not claim cross-process exclusion.

    The caller must serialize reviews against journal writers, including the
    order-snapshot/fill persistence interval. Separate repository reads are not
    an atomic database snapshot; an interleaved or incomplete journal fails
    visibly and must be retried only after journal synchronization.
    """

    def __init__(
        self,
        repository: ExecutionAuditRepository,
        clock: Clock,
        *,
        maximum_records: int = 5000,
        maximum_seconds: float = 15,
    ) -> None:
        if (
            isinstance(maximum_records, bool)
            or not isinstance(maximum_records, int)
            or not 1 <= maximum_records <= 9999
            or isinstance(maximum_seconds, bool)
            or not isinstance(maximum_seconds, (float, int))
            or not math.isfinite(maximum_seconds)
            or maximum_seconds <= 0
        ):
            raise ValueError("review bounds require 1..9999 records and a positive timeout")
        if not repository.binding.idempotency_namespace:
            raise ValueError("review requires an explicit execution namespace")
        self.repository, self.clock = repository, clock
        self.binding = repository.binding
        self.maximum_records, self.maximum_seconds = maximum_records, maximum_seconds
        self._lock = asyncio.Lock()

    def _payload(self, row: dict[str, Any]) -> dict[str, Any]:
        if (
            row.get("environment") != self.binding.environment.value
            or row.get("namespace") != self.binding.idempotency_namespace
        ):
            raise SafetyCriticalError("review read crossed its environment/namespace binding")
        payload = row.get("payload")
        if not isinstance(payload, dict):
            raise DataInvalidError("review evidence payload is not an object")
        if (
            payload.get("environment", self.binding.environment.value)
            != self.binding.environment.value
        ):
            raise SafetyCriticalError("review payload crossed its environment binding")
        if (
            payload.get("namespace", self.binding.idempotency_namespace)
            != self.binding.idempotency_namespace
        ):
            raise SafetyCriticalError("review payload crossed its namespace binding")
        return payload

    async def _scan(self, table: str) -> list[dict[str, Any]]:
        rows = await self.repository.list_payloads(
            table,
            filters={"record_kind": OUTCOME_KIND} if table == "trade_outcomes" else None,
            limit=self.maximum_records + 1,
        )
        if len(rows) > self.maximum_records:
            raise DataInvalidError(f"bounded review scan incomplete: {table}")
        sequences: set[int] = set()
        for row in rows:
            self._payload(row)
            sequence = row.get("append_sequence")
            if isinstance(sequence, bool) or not isinstance(sequence, int) or sequence <= 0:
                raise DataInvalidError("review requires a durable positive append sequence")
            if sequence in sequences:
                raise DataInvalidError("review evidence repeats a durable append sequence")
            sequences.add(sequence)
        return rows

    async def tick(self) -> tuple[ClosedTradeOutcome, ...]:
        async with self._lock, asyncio.timeout(self.maximum_seconds):
            now = _aware(self.clock.now())
            order_rows, fill_rows, outcome_rows = await asyncio.gather(
                self._scan("orders"), self._scan("fills"), self._scan("trade_outcomes")
            )
            try:
                outcomes = self._derive(order_rows, fill_rows, now)
                existing: dict[str, ClosedTradeOutcome] = {}
                for row in outcome_rows:
                    outcome = ClosedTradeOutcome.model_validate(self._payload(row))
                    previous = existing.get(outcome.outcome_id)
                    if previous is not None:
                        self._compare(previous, outcome)
                    existing[outcome.outcome_id] = outcome
                # Validate every derived result against existing immutable records
                # before any append. A late contradictory fill cannot overwrite history.
                for outcome in outcomes:
                    if prior := existing.get(outcome.outcome_id):
                        self._compare(prior, outcome)
                derived_ids = {outcome.outcome_id for outcome in outcomes}
                if any(
                    prior.closed_at <= now and prior.outcome_id not in derived_ids
                    for prior in existing.values()
                ):
                    raise DataInvalidError(
                        "persisted closed outcome is not reproduced by the journal"
                    )
            except (ValidationError, ValueError, TypeError, KeyError) as exc:
                raise DataInvalidError("closed-position review evidence is invalid") from exc
            appended: list[ClosedTradeOutcome] = []
            for outcome in outcomes:
                if outcome.outcome_id in existing:
                    continue
                try:
                    await self.repository.append("trade_outcomes", outcome)
                except IntegrityError:
                    # Only an identical committed record proves a successful race.
                    existing_row = await self.repository.find_payload(
                        "trade_outcomes", "outcome_id", outcome.outcome_id
                    )
                    if existing_row is None:
                        raise
                    self._compare(
                        ClosedTradeOutcome.model_validate(self._payload(existing_row)), outcome
                    )
                    continue
                appended.append(outcome)
            return tuple(appended)

    @staticmethod
    def _compare(previous: ClosedTradeOutcome, current: ClosedTradeOutcome) -> None:
        if previous.model_dump(exclude={"reviewed_at"}) != current.model_dump(
            exclude={"reviewed_at"}
        ):
            raise DataInvalidError("closed-position outcome conflicts with immutable evidence")

    def _derive(
        self, order_rows: list[dict[str, Any]], fill_rows: list[dict[str, Any]], now: datetime
    ) -> tuple[ClosedTradeOutcome, ...]:
        orders: dict[UUID, BrokerOrder] = {}
        recorded_filled: dict[UUID, int] = defaultdict(int)
        for row in order_rows:
            payload = self._payload(row)
            if _timestamp(row["created_at"]) > now or _timestamp(payload.get("created_at")) > now:
                continue
            order = BrokerOrder.model_validate(payload)
            if order.submitted_at is not None and _aware(order.submitted_at) > now:
                continue
            if order.filled_quantity > order.quantity:
                raise DataInvalidError("broker order has an impossible filled quantity")
            if previous := orders.get(order.order_id):
                if _order_terms(previous) != _order_terms(order):
                    raise DataInvalidError("broker order identity has inconsistent exact terms")
            orders[order.order_id] = order
            recorded_filled[order.order_id] = max(
                recorded_filled[order.order_id], order.filled_quantity
            )

        fills: dict[UUID, tuple[Fill, int]] = {}
        for row in fill_rows:
            payload = self._payload(row)
            if _timestamp(row["created_at"]) > now or _timestamp(payload.get("created_at")) > now:
                continue
            fill = Fill.model_validate(payload)
            if previous_fill := fills.get(fill.fill_id):
                if previous_fill[0] != fill:
                    raise DataInvalidError("fill identity has inconsistent immutable content")
                fills[fill.fill_id] = (fill, min(previous_fill[1], row["append_sequence"]))
            else:
                fills[fill.fill_id] = (fill, row["append_sequence"])

        by_order: dict[UUID, int] = defaultdict(int)
        filled_intents: dict[UUID, UUID] = {}
        contracts: dict[str, OptionContract] = {}
        for fill, _ in fills.values():
            matched_order = orders.get(fill.order_id)
            if matched_order is None:
                raise DataInvalidError("fill has no causally available durable broker order")
            order = matched_order
            if order.created_at > fill.created_at or (
                order.submitted_at is not None and _aware(order.submitted_at) > fill.created_at
            ):
                raise DataInvalidError("fill precedes its broker order")
            if filled_intents.get(order.intent_id, order.order_id) != order.order_id:
                raise DataInvalidError("one intent has conflicting filled broker identities")
            filled_intents[order.intent_id] = order.order_id
            contract = contracts.setdefault(order.contract.instrument_id, order.contract)
            if contract != order.contract:
                raise DataInvalidError("instrument identity has inconsistent contract terms")
            by_order[order.order_id] += fill.quantity
        for order_id, order in orders.items():
            if (
                by_order[order_id] != recorded_filled[order_id]
                or by_order[order_id] > order.quantity
            ):
                raise DataInvalidError(
                    "durable fills do not reconcile to broker order filled quantity"
                )

        cycles: dict[str, _Cycle] = {}
        completed: list[ClosedTradeOutcome] = []
        with localcontext() as context:
            context.prec = 50
            context.rounding = ROUND_HALF_EVEN
            for fill, _ in sorted(fills.values(), key=lambda pair: (pair[0].created_at, pair[1])):
                order = orders[fill.order_id]
                instrument = order.contract.instrument_id
                cycle = cycles.setdefault(instrument, _Cycle(order.contract))
                cycle.fills.append(fill)
                if order.side is OrderSide.BUY_TO_OPEN:
                    cycle.entries.append(fill)
                    cycle.balance += fill.quantity
                else:
                    if fill.quantity > cycle.balance:
                        raise DataInvalidError(
                            "exit fill oversells or lacks an opening long position"
                        )
                    cycle.exits.append(fill)
                    cycle.balance -= fill.quantity
                if cycle.balance == 0:
                    completed.append(self._outcome(cycle, orders, now))
                    del cycles[instrument]
        return tuple(completed)

    def _outcome(
        self, cycle: _Cycle, orders: dict[UUID, BrokerOrder], now: datetime
    ) -> ClosedTradeOutcome:
        multiplier = cycle.contract.multiplier
        cost = sum((fill.price * fill.quantity * multiplier for fill in cycle.entries), Decimal(0))
        proceeds = sum(
            (fill.price * fill.quantity * multiplier for fill in cycle.exits), Decimal(0)
        )
        pnl = proceeds - cost
        opened_at, closed_at = cycle.entries[0].created_at, cycle.exits[-1].created_at
        elapsed = closed_at - opened_at
        hold = (
            Decimal(elapsed.days * 86400 + elapsed.seconds)
            + Decimal(elapsed.microseconds) / 1000000
        )
        cycle_id = sha256_json(
            {
                "environment": self.binding.environment.value,
                "namespace": self.binding.idempotency_namespace,
                "instrument_id": cycle.contract.instrument_id,
                "first_entry_fill_id": str(cycle.entries[0].fill_id),
            }
        )
        order_ids = tuple(dict.fromkeys(fill.order_id for fill in cycle.fills))
        evidence = {
            "fills": [fill.model_dump(mode="json") for fill in cycle.fills],
            "order_terms": [_order_terms(orders[order_id]) for order_id in order_ids],
        }
        return ClosedTradeOutcome(
            created_at=closed_at,
            outcome_id=sha256_json({"cycle_id": cycle_id, "version": OUTCOME_VERSION}),
            cycle_id=cycle_id,
            environment=self.binding.environment,
            namespace=self.binding.idempotency_namespace,
            contract=cycle.contract,
            opened_at=opened_at,
            closed_at=closed_at,
            reviewed_at=now,
            entry_fill_ids=tuple(fill.fill_id for fill in cycle.entries),
            exit_fill_ids=tuple(fill.fill_id for fill in cycle.exits),
            order_ids=order_ids,
            contracts_opened=sum(fill.quantity for fill in cycle.entries),
            contracts_closed=sum(fill.quantity for fill in cycle.exits),
            gross_entry_cost=cost,
            gross_exit_proceeds=proceeds,
            gross_realized_pnl=pnl,
            gross_return_percent=(pnl / cost * 100).quantize(Decimal("0.000000000001")),
            hold_seconds=hold,
            source_evidence_hash=sha256_json(evidence),
        )
