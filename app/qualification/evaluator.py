from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, date, datetime, time
from decimal import Decimal
from enum import StrEnum
from pathlib import Path
from typing import Literal
from uuid import UUID, uuid4
from zoneinfo import ZoneInfo

from pydantic import Field, field_validator, model_validator

from app.clock.market_calendar import CalendarCoverageError, UsEquityCalendar
from app.domain.enums import DemoBackend, ExecutionEnvironment
from app.domain.models import DomainModel, TimestampedModel, canonical_json

_NEW_YORK = ZoneInfo("America/New_York")
_SHA256_PATTERN = r"^[a-f0-9]{64}$"


class GateStatus(StrEnum):
    PASS = "PASS"  # noqa: S105 -- gate result, not a password
    PENDING = "PENDING"
    FAIL = "FAIL"


class QualificationStatus(StrEnum):
    NOT_STARTED = "NOT_STARTED"
    IN_PROGRESS = "IN_PROGRESS"
    FAILED = "FAILED"
    PASSED = "PASSED"


class HealthCoverage(DomainModel):
    """Bounded session health evidence, not a claim inferred from one final check."""

    first_check_at: datetime
    last_check_at: datetime
    checks: int = Field(gt=0)
    healthy_checks: int = Field(ge=0)
    recovered_failures: int = Field(default=0, ge=0)

    @field_validator("first_check_at", "last_check_at")
    @classmethod
    def timestamps_are_utc(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("coverage timestamps must be timezone-aware")
        return value.astimezone(UTC)

    @model_validator(mode="after")
    def counts_and_order_are_valid(self) -> HealthCoverage:
        if self.last_check_at < self.first_check_at:
            raise ValueError("health coverage ends before it begins")
        if self.healthy_checks + self.recovered_failures != self.checks:
            raise ValueError("every health check must be healthy or a recovered failure")
        return self

    def covers(self, opened_at: datetime, closed_at: datetime) -> bool:
        return self.first_check_at <= opened_at and self.last_check_at >= closed_at


class SessionEvidence(TimestampedModel):
    """Final evidence for one real, authenticated broker-shadow market session.

    This record intentionally cannot represent replay or OFFLINE_SIM. It should only be
    written after the scheduled regular close and end-of-day reconciliation have completed.
    """

    session_id: UUID = Field(default_factory=uuid4)
    environment: Literal[ExecutionEnvironment.DEMO] = ExecutionEnvironment.DEMO
    demo_backend: Literal[DemoBackend.BROKER_SHADOW] = DemoBackend.BROKER_SHADOW
    namespace: str = Field(min_length=1, max_length=128)
    evidence_origin: Literal["AUTHENTICATED_CURRENT"] = "AUTHENTICATED_CURRENT"
    regular_session_date: date
    calendar_version: str
    premarket_observed_at: datetime
    regular_market_open_observed_at: datetime
    regular_market_close_observed_at: datetime
    eod_completed_at: datetime

    account_fingerprint: str = Field(min_length=8, max_length=256)
    authenticated: bool
    capability_snapshot_id: UUID | None
    mcp_protocol_version: str | None = Field(default=None, max_length=80)
    capability_catalog_version: str | None = Field(default=None, max_length=120)
    capability_catalog_hash: str | None = Field(default=None, pattern=_SHA256_PATTERN)
    command_schema_hashes: dict[str, str] = Field(default_factory=dict)
    capability_drift_reviewed: bool = False

    public_source_health: HealthCoverage
    market_data_health: HealthCoverage
    broker_read_health: HealthCoverage

    real_broker_account_snapshot_ids: tuple[UUID, ...]
    real_cash: Decimal = Field(ge=0)
    real_buying_power: Decimal = Field(ge=0)
    real_open_positions: int = Field(ge=0)
    real_open_orders: int = Field(ge=0)
    unexpected_real_deposit: bool = False

    shadow_account_snapshot_ids: tuple[UUID, ...]
    shadow_cash: Decimal = Field(ge=0)
    shadow_buying_power: Decimal = Field(ge=0)
    shadow_realized_pnl: Decimal = Decimal("0")
    shadow_unrealized_pnl: Decimal = Decimal("0")

    natural_command_intent_ids: tuple[UUID, ...] = ()
    plumbing_command_intent_ids: tuple[UUID, ...] = ()
    policy_counterfactuals_required: int = Field(default=0, ge=0)
    policy_counterfactual_ids: tuple[UUID, ...] = ()

    reconciliation_event_ids: tuple[UUID, ...]
    eod_reconciled: bool
    restart_event_ids: tuple[UUID, ...] = ()
    restart_reconciled: bool = False
    transient_failure_event_ids: tuple[UUID, ...] = ()
    transient_failure_recovered_safely: bool = False
    reconnect_exercised: bool = False
    unresolved_safety_incidents: tuple[str, ...] = ()

    @field_validator(
        "premarket_observed_at",
        "regular_market_open_observed_at",
        "regular_market_close_observed_at",
        "eod_completed_at",
    )
    @classmethod
    def session_timestamps_are_utc(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("session timestamps must be timezone-aware")
        return value.astimezone(UTC)

    @field_validator("command_schema_hashes")
    @classmethod
    def schema_hashes_are_canonical(cls, value: dict[str, str]) -> dict[str, str]:
        if any(not name.strip() for name in value):
            raise ValueError("capability names cannot be empty")
        if any(
            len(digest) != 64
            or any(ch not in "0123456789abcdef" for ch in digest)
            for digest in value.values()
        ):
            raise ValueError("capability schema hashes must be lowercase SHA-256")
        return value

    @model_validator(mode="after")
    def identifiers_and_times_are_consistent(self) -> SessionEvidence:
        identity_groups = (
            self.real_broker_account_snapshot_ids,
            self.shadow_account_snapshot_ids,
            self.natural_command_intent_ids,
            self.plumbing_command_intent_ids,
            self.policy_counterfactual_ids,
            self.reconciliation_event_ids,
            self.restart_event_ids,
            self.transient_failure_event_ids,
        )
        if any(len(values) != len(set(values)) for values in identity_groups):
            raise ValueError("qualification evidence identifiers must be unique")
        if set(self.natural_command_intent_ids) & set(self.plumbing_command_intent_ids):
            raise ValueError("an intent cannot be both natural and plumbing-only evidence")
        local_times = (
            self.premarket_observed_at,
            self.regular_market_open_observed_at,
            self.regular_market_close_observed_at,
            self.eod_completed_at,
        )
        if any(
            value.astimezone(_NEW_YORK).date() != self.regular_session_date
            for value in local_times
        ):
            raise ValueError("session evidence timestamps must share the regular-session date")
        if not (
            self.premarket_observed_at
            <= self.regular_market_open_observed_at
            <= self.regular_market_close_observed_at
            <= self.eod_completed_at
        ):
            raise ValueError("qualification session timestamps are out of order")
        return self

    @property
    def command_intent_ids(self) -> tuple[UUID, ...]:
        return self.natural_command_intent_ids + self.plumbing_command_intent_ids


class AuditCompleteness(DomainModel):
    declared_command_intents: int = Field(ge=0)
    natural_command_intents: int = Field(ge=0)
    plumbing_command_intents: int = Field(ge=0)
    complete_command_intents: int = Field(ge=0)
    side_effect_free_reviews: int = Field(ge=0)
    firewall_denials: int = Field(ge=0)
    transmitted_external_writes: int = Field(ge=0)
    undeclared_command_intents: int = Field(ge=0)
    real_account_breaches: int = Field(default=0, ge=0)
    account_separation_issues: int = Field(default=0, ge=0)
    issues: tuple[str, ...] = ()


class QualificationGate(DomainModel):
    code: str
    status: GateStatus
    detail: str


class QualificationReport(TimestampedModel):
    qualification_id: UUID = Field(default_factory=uuid4)
    evaluator_version: str
    status: QualificationStatus
    account_fingerprint: str | None
    sessions_observed: int
    required_sessions: int
    session_dates: tuple[date, ...]
    gates: tuple[QualificationGate, ...]
    warnings: tuple[str, ...]
    totals: dict[str, int]
    session_ids: tuple[UUID, ...]
    current_real_broker: dict[str, str | int | bool | None]
    current_shadow_ledger: dict[str, str | int | bool | None]
    capability: dict[str, str | int | bool | None]
    audit_issues: tuple[str, ...] = ()

    @property
    def passed(self) -> bool:
        return self.status is QualificationStatus.PASSED

    @property
    def failed_gates(self) -> tuple[str, ...]:
        return tuple(gate.code for gate in self.gates if gate.status is GateStatus.FAIL)

    def public_payload(self) -> dict[str, object]:
        payload = self.model_dump(mode="json", exclude={"account_fingerprint"})
        fingerprint = self.account_fingerprint
        payload["account_identity"] = {
            "present": fingerprint is not None,
            "stable": self._gate_status("same_account_continuity") is GateStatus.PASS,
            "masked_fingerprint": (
                f"{fingerprint[:8]}…{fingerprint[-4:]}" if fingerprint is not None else None
            ),
        }
        payload["passed"] = self.passed
        payload["failed_gates"] = list(self.failed_gates)
        return payload

    def _gate_status(self, code: str) -> GateStatus | None:
        return next((gate.status for gate in self.gates if gate.code == code), None)

    def write_json(self, destination: Path) -> None:
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(
            canonical_json(self.model_dump(mode="json")) + "\n", encoding="utf-8"
        )


class QualificationEvaluator:
    version = "broker-shadow-qualification-v2"

    def __init__(
        self,
        required_regular_sessions: int = 5,
        *,
        calendar: UsEquityCalendar | None = None,
    ) -> None:
        if required_regular_sessions < 5:
            raise ValueError("broker-shadow qualification requires at least five sessions")
        self.required_regular_sessions = required_regular_sessions
        self.calendar = calendar or UsEquityCalendar()

    def evaluate(
        self,
        sessions: list[SessionEvidence],
        at: datetime,
        *,
        audit: AuditCompleteness | None = None,
        invalid_records: tuple[str, ...] = (),
    ) -> QualificationReport:
        if at.tzinfo is None or at.utcoffset() is None:
            raise ValueError("qualification evaluation time must be timezone-aware")
        ordered = sorted(
            sessions, key=lambda session: (session.regular_session_date, session.session_id.hex)
        )
        gates: list[QualificationGate] = []
        warnings: list[str] = []

        def gate(code: str, state: GateStatus, detail: str) -> None:
            gates.append(QualificationGate(code=code, status=state, detail=detail))

        if invalid_records:
            gate(
                "session_record_integrity",
                GateStatus.FAIL,
                f"{len(invalid_records)} stored session record(s) are malformed or duplicated",
            )
        elif ordered:
            gate(
                "session_record_integrity",
                GateStatus.PASS,
                "all stored session summaries validate",
            )
        else:
            gate(
                "session_record_integrity",
                GateStatus.PENDING,
                "no session summary has been recorded",
            )

        calendar_issues = self._calendar_issues(ordered)
        unique_dates = tuple(sorted({session.regular_session_date for session in ordered}))
        if calendar_issues:
            gate("full_regular_sessions", GateStatus.FAIL, "; ".join(calendar_issues))
        elif not ordered:
            gate(
                "full_regular_sessions",
                GateStatus.PENDING,
                "waiting for authenticated current-market sessions",
            )
        elif len(unique_dates) < self.required_regular_sessions:
            gate(
                "full_regular_sessions",
                GateStatus.PENDING,
                f"{len(unique_dates)}/{self.required_regular_sessions} complete regular sessions",
            )
        else:
            gate(
                "full_regular_sessions",
                GateStatus.PASS,
                f"{len(unique_dates)}/{self.required_regular_sessions} complete regular sessions",
            )

        fingerprints = {session.account_fingerprint for session in ordered}
        if not ordered:
            gate(
                "same_account_continuity",
                GateStatus.PENDING,
                "intended account has not been observed",
            )
        elif len(fingerprints) != 1:
            gate(
                "same_account_continuity",
                GateStatus.FAIL,
                "account fingerprint changed during qualification",
            )
        else:
            gate(
                "same_account_continuity",
                GateStatus.PASS,
                "one masked account identity spans all sessions",
            )

        missing_capability = [
            session
            for session in ordered
            if not session.authenticated
            or session.capability_snapshot_id is None
            or not session.mcp_protocol_version
            or not session.capability_catalog_version
            or not session.capability_catalog_hash
            or not session.command_schema_hashes
        ]
        drift_unreviewed = any(
            current.capability_catalog_hash != previous.capability_catalog_hash
            and not current.capability_drift_reviewed
            for previous, current in zip(ordered, ordered[1:], strict=False)
        )
        if not ordered:
            gate(
                "authenticated_mcp_capabilities",
                GateStatus.PENDING,
                "authentication and discovery have not run",
            )
        elif missing_capability or drift_unreviewed:
            gate(
                "authenticated_mcp_capabilities",
                GateStatus.FAIL,
                "authentication/capability schema evidence is incomplete or drift is unreviewed",
            )
        else:
            gate(
                "authenticated_mcp_capabilities",
                GateStatus.PASS,
                "authenticated schema snapshots are complete",
            )

        coverage_failures = self._coverage_failures(ordered)
        if not ordered:
            gate(
                "continuous_health_coverage",
                GateStatus.PENDING,
                "source, market, and broker coverage is pending",
            )
        elif coverage_failures:
            gate("continuous_health_coverage", GateStatus.FAIL, "; ".join(coverage_failures))
        else:
            gate(
                "continuous_health_coverage",
                GateStatus.PASS,
                "source, market, and broker checks cover every session",
            )

        funding_breaches = [
            session
            for session in ordered
            if session.real_cash != 0
            or session.real_buying_power != 0
            or session.real_open_positions
            or session.real_open_orders
            or session.unexpected_real_deposit
        ]
        missing_accounts = [
            session
            for session in ordered
            if not session.real_broker_account_snapshot_ids
            or not session.shadow_account_snapshot_ids
        ]
        if not ordered:
            gate(
                "real_account_unfunded",
                GateStatus.PENDING,
                "real broker account has not been observed",
            )
            gate(
                "real_shadow_account_separation",
                GateStatus.PENDING,
                "both account ledgers are pending",
            )
        else:
            gate(
                "real_account_unfunded",
                GateStatus.FAIL
                if funding_breaches or (audit is not None and audit.real_account_breaches)
                else GateStatus.PASS,
                "unexpected real funds/order/position detected"
                if funding_breaches
                or (audit is not None and audit.real_account_breaches)
                else "real observed cash, buying power, positions, and open orders remain zero",
            )
            gate(
                "real_shadow_account_separation",
                GateStatus.FAIL
                if missing_accounts or (audit is not None and audit.account_separation_issues)
                else GateStatus.PASS,
                "real or shadow account snapshots are missing"
                if missing_accounts
                or (audit is not None and audit.account_separation_issues)
                else "real and shadow account snapshots are separately identified",
            )

        total_declared = sum(len(session.command_intent_ids) for session in ordered)
        if audit is None or total_declared == 0:
            gate(
                "exact_command_audit",
                GateStatus.PENDING,
                "no complete exact command-intent path is yet proven",
            )
            gate(
                "shadow_write_firewall",
                GateStatus.PENDING,
                "no durable deny-all command path is yet proven",
            )
        else:
            exact_ok = (
                not audit.issues
                and audit.complete_command_intents == audit.declared_command_intents
                and audit.undeclared_command_intents == 0
            )
            firewall_ok = (
                audit.transmitted_external_writes == 0
                and audit.firewall_denials == audit.declared_command_intents
            )
            gate(
                "exact_command_audit",
                GateStatus.PASS if exact_ok else GateStatus.FAIL,
                f"{audit.complete_command_intents}/{audit.declared_command_intents} "
                "command paths have complete durable linkage",
            )
            gate(
                "shadow_write_firewall",
                GateStatus.PASS if firewall_ok else GateStatus.FAIL,
                f"{audit.firewall_denials} denied, {audit.transmitted_external_writes} transmitted",
            )

        required_counterfactuals = sum(
            session.policy_counterfactuals_required for session in ordered
        )
        recorded_counterfactuals = sum(
            len(session.policy_counterfactual_ids) for session in ordered
        )
        if not ordered or total_declared == 0:
            counterfactual_state = GateStatus.PENDING
        elif recorded_counterfactuals != required_counterfactuals:
            counterfactual_state = GateStatus.FAIL
        else:
            counterfactual_state = GateStatus.PASS
        gate(
            "live_policy_counterfactuals",
            counterfactual_state,
            f"{recorded_counterfactuals}/{required_counterfactuals} required decisions recorded",
        )

        self._period_gate(
            gate,
            "restart_reconciliation",
            any(session.restart_event_ids and session.restart_reconciled for session in ordered),
            "a process/service restart reconciled",
        )
        self._period_gate(
            gate,
            "transient_failure_recovery",
            any(
                session.transient_failure_event_ids
                and session.transient_failure_recovered_safely
                for session in ordered
            ),
            "a transient provider failure recovered safely",
        )
        self._period_gate(
            gate,
            "authenticated_mcp_reconnect",
            any(session.reconnect_exercised for session in ordered),
            "the authenticated MCP reconnect path was exercised",
        )

        unreconciled = [
            session
            for session in ordered
            if not session.eod_reconciled or not session.reconciliation_event_ids
        ]
        incidents = [
            item for session in ordered for item in session.unresolved_safety_incidents
        ]
        if not ordered:
            gate(
                "reconciliation_and_incidents",
                GateStatus.PENDING,
                "end-of-day reconciliation is pending",
            )
        elif unreconciled or incidents:
            gate(
                "reconciliation_and_incidents",
                GateStatus.FAIL,
                f"{len(unreconciled)} unreconciled session(s), "
                f"{len(incidents)} unresolved incident(s)",
            )
        else:
            gate(
                "reconciliation_and_incidents",
                GateStatus.PASS,
                "all sessions reconciled with no unresolved incident",
            )

        if total_declared and audit is not None and audit.natural_command_intents == 0:
            warnings.append(
                "command-path coverage is plumbing-only; it is not market-performance evidence"
            )

        status = self._status(ordered, gates)
        current = ordered[-1] if ordered else None
        account_fingerprint = next(iter(fingerprints)) if len(fingerprints) == 1 else None
        return QualificationReport(
            created_at=at,
            evaluator_version=self.version,
            status=status,
            account_fingerprint=account_fingerprint,
            sessions_observed=len(unique_dates),
            required_sessions=self.required_regular_sessions,
            session_dates=unique_dates,
            gates=tuple(gates),
            warnings=tuple(warnings),
            totals={
                "natural_command_intents": audit.natural_command_intents if audit else 0,
                "plumbing_command_intents": audit.plumbing_command_intents if audit else 0,
                "complete_command_intents": audit.complete_command_intents if audit else 0,
                "side_effect_free_reviews": audit.side_effect_free_reviews if audit else 0,
                "firewall_denials": audit.firewall_denials if audit else 0,
                "external_writes_transmitted": audit.transmitted_external_writes if audit else 0,
                "policy_counterfactuals_required": required_counterfactuals,
                "policy_counterfactuals_recorded": recorded_counterfactuals,
                "restart_events": sum(len(session.restart_event_ids) for session in ordered),
                "reconciliation_events": sum(
                    len(session.reconciliation_event_ids) for session in ordered
                ),
                "unresolved_incidents": len(incidents),
            },
            session_ids=tuple(session.session_id for session in ordered),
            current_real_broker={
                "observed": current is not None,
                "cash": str(current.real_cash) if current else None,
                "buying_power": str(current.real_buying_power) if current else None,
                "open_positions": current.real_open_positions if current else None,
                "open_orders": current.real_open_orders if current else None,
                "unexpected_deposit": current.unexpected_real_deposit if current else None,
            },
            current_shadow_ledger={
                "observed": current is not None,
                "cash": str(current.shadow_cash) if current else None,
                "buying_power": str(current.shadow_buying_power) if current else None,
                "realized_pnl": str(current.shadow_realized_pnl) if current else None,
                "unrealized_pnl": str(current.shadow_unrealized_pnl) if current else None,
            },
            capability={
                "snapshot_present": bool(current and current.capability_snapshot_id),
                "mcp_protocol_version": current.mcp_protocol_version if current else None,
                "catalog_version": current.capability_catalog_version if current else None,
                "catalog_hash": current.capability_catalog_hash if current else None,
                "command_schema_count": len(current.command_schema_hashes) if current else 0,
            },
            audit_issues=audit.issues if audit else (),
        )

    def _calendar_issues(self, sessions: list[SessionEvidence]) -> list[str]:
        issues: list[str] = []
        seen: set[date] = set()
        for session in sessions:
            day = session.regular_session_date
            if day in seen:
                issues.append(f"duplicate finalized session date {day.isoformat()}")
                continue
            seen.add(day)
            try:
                close = self.calendar.regular_session_close(day)
            except CalendarCoverageError:
                issues.append(f"unverified exchange calendar for {day.isoformat()}")
                continue
            if close is None:
                issues.append(f"{day.isoformat()} is not a scheduled regular session")
                continue
            opened = datetime.combine(day, time(9, 30), tzinfo=_NEW_YORK).astimezone(UTC)
            closed = close.astimezone(UTC)
            if session.calendar_version != self.calendar.schedule_version:
                issues.append(f"calendar version mismatch on {day.isoformat()}")
            if session.premarket_observed_at >= opened:
                issues.append(f"premarket startup missing on {day.isoformat()}")
            if session.regular_market_open_observed_at > opened:
                issues.append(f"regular open coverage started late on {day.isoformat()}")
            if session.regular_market_close_observed_at < closed:
                issues.append(f"regular close coverage ended early on {day.isoformat()}")
            if session.eod_completed_at < closed:
                issues.append(f"end-of-day completion predates close on {day.isoformat()}")
        return issues

    def _coverage_failures(self, sessions: list[SessionEvidence]) -> list[str]:
        failures: list[str] = []
        for session in sessions:
            try:
                close = self.calendar.regular_session_close(session.regular_session_date)
            except CalendarCoverageError:
                continue
            if close is None:
                continue
            opened = datetime.combine(
                session.regular_session_date, time(9, 30), tzinfo=_NEW_YORK
            ).astimezone(UTC)
            closed = close.astimezone(UTC)
            for label, coverage in (
                ("official sources", session.public_source_health),
                ("market data", session.market_data_health),
                ("broker reads", session.broker_read_health),
            ):
                if not coverage.covers(opened, closed):
                    failures.append(
                        f"{label} coverage incomplete on {session.regular_session_date}"
                    )
        return failures

    @staticmethod
    def _period_gate(
        add_gate: Callable[[str, GateStatus, str], None],
        code: str,
        completed: bool,
        completed_detail: str,
    ) -> None:
        add_gate(
            code,
            GateStatus.PASS if completed else GateStatus.PENDING,
            completed_detail if completed else f"pending: {completed_detail}",
        )

    @staticmethod
    def _status(
        sessions: list[SessionEvidence], gates: list[QualificationGate]
    ) -> QualificationStatus:
        if not sessions and not any(gate.status is GateStatus.FAIL for gate in gates):
            return QualificationStatus.NOT_STARTED
        if any(gate.status is GateStatus.FAIL for gate in gates):
            return QualificationStatus.FAILED
        if any(gate.status is GateStatus.PENDING for gate in gates):
            return QualificationStatus.IN_PROGRESS
        return QualificationStatus.PASSED
