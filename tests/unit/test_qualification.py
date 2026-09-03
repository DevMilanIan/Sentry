from __future__ import annotations

from datetime import date, datetime, timedelta
from uuid import uuid4

from app.qualification.evaluator import QualificationEvaluator, SessionEvidence


def _session(day: date, instant: datetime) -> SessionEvidence:
    return SessionEvidence(
        created_at=instant,
        regular_session_date=day,
        account_fingerprint="masked-fingerprint",
        authenticated=True,
        capability_snapshot_id=uuid4(),
        premarket_observed=True,
        regular_market_observed=True,
        eod_completed=True,
        reconnect_exercised=day.day == 1,
        restart_reconciled=day.day == 2,
        transient_failure_recovered_safely=day.day == 3,
        command_intents=1,
        complete_command_intents=1,
        transmitted_external_writes=0,
        firewall_denials=1,
        real_shadow_state_conflations=0,
        policy_counterfactuals_required=1,
        policy_counterfactuals_recorded=1,
    )


def test_five_complete_sessions_pass(instant: datetime) -> None:
    sessions = [_session(date(2026, 9, day), instant + timedelta(days=day)) for day in range(1, 6)]
    report = QualificationEvaluator().evaluate(sessions, instant + timedelta(days=6))
    assert report.passed
    assert not report.failed_gates


def test_any_external_write_fails_qualification(instant: datetime) -> None:
    sessions = [_session(date(2026, 9, day), instant + timedelta(days=day)) for day in range(1, 6)]
    sessions[0] = sessions[0].model_copy(update={"transmitted_external_writes": 1})
    report = QualificationEvaluator().evaluate(sessions, instant + timedelta(days=6))
    assert not report.passed
    assert "external_write_transmitted" in report.failed_gates
