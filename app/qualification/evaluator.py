from __future__ import annotations

from datetime import date, datetime
from pathlib import Path
from uuid import UUID, uuid4

from pydantic import Field, model_validator

from app.domain.models import TimestampedModel, canonical_json


class SessionEvidence(TimestampedModel):
    session_id: UUID = Field(default_factory=uuid4)
    regular_session_date: date
    account_fingerprint: str
    authenticated: bool
    capability_snapshot_id: UUID | None
    premarket_observed: bool
    regular_market_observed: bool
    eod_completed: bool
    reconnect_exercised: bool = False
    restart_reconciled: bool = False
    transient_failure_recovered_safely: bool = False
    command_intents: int = Field(ge=0)
    complete_command_intents: int = Field(ge=0)
    transmitted_external_writes: int = Field(ge=0)
    firewall_denials: int = Field(ge=0)
    real_shadow_state_conflations: int = Field(ge=0)
    policy_counterfactuals_required: int = Field(ge=0)
    policy_counterfactuals_recorded: int = Field(ge=0)
    safety_incidents: tuple[str, ...] = ()

    @model_validator(mode="after")
    def counts_are_consistent(self) -> SessionEvidence:
        if self.complete_command_intents > self.command_intents:
            raise ValueError("complete intent count exceeds total")
        if self.firewall_denials > self.command_intents:
            raise ValueError("firewall denial count exceeds command intents")
        if self.policy_counterfactuals_recorded > self.policy_counterfactuals_required:
            raise ValueError("counterfactual count exceeds required")
        return self


class QualificationReport(TimestampedModel):
    qualification_id: UUID = Field(default_factory=uuid4)
    account_fingerprint: str | None
    sessions_observed: int
    required_sessions: int
    passed: bool
    failed_gates: tuple[str, ...]
    warnings: tuple[str, ...]
    totals: dict[str, int]
    session_ids: tuple[UUID, ...]

    def write_json(self, destination: Path) -> None:
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(
            canonical_json(self.model_dump(mode="json")) + "\n", encoding="utf-8"
        )


class QualificationEvaluator:
    version = "broker-shadow-qualification-v1"

    def __init__(self, required_regular_sessions: int = 5) -> None:
        if required_regular_sessions < 1:
            raise ValueError("qualification requires at least one session")
        self.required_regular_sessions = required_regular_sessions

    def evaluate(self, sessions: list[SessionEvidence], at: datetime) -> QualificationReport:
        failures: list[str] = []
        warnings: list[str] = []
        unique_dates = {
            session.regular_session_date for session in sessions if session.regular_market_observed
        }
        fingerprints = {session.account_fingerprint for session in sessions}
        total_intents = sum(session.command_intents for session in sessions)
        complete_intents = sum(session.complete_command_intents for session in sessions)
        writes = sum(session.transmitted_external_writes for session in sessions)
        denials = sum(session.firewall_denials for session in sessions)
        conflations = sum(session.real_shadow_state_conflations for session in sessions)
        counterfactuals_required = sum(
            session.policy_counterfactuals_required for session in sessions
        )
        counterfactuals_recorded = sum(
            session.policy_counterfactuals_recorded for session in sessions
        )
        incidents = [incident for session in sessions for incident in session.safety_incidents]

        if len(unique_dates) < self.required_regular_sessions:
            failures.append("insufficient_regular_market_sessions")
        if len(fingerprints) != 1 or not fingerprints:
            failures.append("account_identity_not_stable")
        if not all(
            session.authenticated and session.capability_snapshot_id for session in sessions
        ):
            failures.append("authentication_or_capability_evidence_incomplete")
        if not all(session.premarket_observed and session.eod_completed for session in sessions):
            failures.append("market_session_coverage_incomplete")
        if writes:
            failures.append("external_write_transmitted")
        if complete_intents != total_intents:
            failures.append("broker_command_intent_incomplete")
        if denials != total_intents:
            failures.append("write_firewall_evidence_incomplete")
        if conflations:
            failures.append("real_and_shadow_account_state_conflated")
        if counterfactuals_recorded != counterfactuals_required:
            failures.append("live_policy_counterfactual_incomplete")
        if incidents:
            failures.append("unresolved_safety_incident")
        if not any(session.restart_reconciled for session in sessions):
            failures.append("restart_reconciliation_not_exercised")
        if not any(session.transient_failure_recovered_safely for session in sessions):
            failures.append("transient_failure_not_exercised")
        if not any(session.reconnect_exercised for session in sessions):
            failures.append("authenticated_reconnect_not_exercised")
        if total_intents == 0:
            warnings.append("no_natural_command_intents; use separately labeled plumbing fixtures")

        return QualificationReport(
            created_at=at,
            account_fingerprint=next(iter(fingerprints)) if len(fingerprints) == 1 else None,
            sessions_observed=len(unique_dates),
            required_sessions=self.required_regular_sessions,
            passed=not failures,
            failed_gates=tuple(failures),
            warnings=tuple(warnings),
            totals={
                "command_intents": total_intents,
                "complete_command_intents": complete_intents,
                "external_writes": writes,
                "firewall_denials": denials,
                "policy_counterfactuals_required": counterfactuals_required,
                "policy_counterfactuals_recorded": counterfactuals_recorded,
            },
            session_ids=tuple(session.session_id for session in sessions),
        )
