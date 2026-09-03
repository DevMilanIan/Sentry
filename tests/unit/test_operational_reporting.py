from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone
from decimal import Decimal

import pytest
from pydantic import ValidationError

from app.domain.enums import DemoBackend, ExecutionEnvironment
from app.reporting.operational import OperationalReportBuilder, OperationalSnapshot


def _snapshot(**overrides: object) -> OperationalSnapshot:
    values: dict[str, object] = {
        "generated_at": datetime(2026, 9, 3, 13, 45, tzinfo=UTC),
        "environment": ExecutionEnvironment.DEMO,
        "demo_backend": DemoBackend.OFFLINE_SIM,
        "market_regime": "risk-on",
        "system_health": "healthy",
        "overnight_catalysts": ("cooler inflation print", "ACME product launch"),
        "federal_changes": ("updated Fed guidance",),
        "earnings_risks": ("ACME after close",),
        "watchlist": ("ACME", "XYZ"),
        "open_positions": 2,
        "open_orders": 1,
        "candidates_considered": 12,
        "rejected_candidates": 9,
        "proposals": 3,
        "realized_pnl": Decimal("12.34"),
        "unrealized_pnl": Decimal("-5.67"),
        "command_intents": 3,
        "firewall_denials": 2,
        "mcp_read_review_errors": 1,
        "policy_divergences": 2,
        "qualification_sessions": 4,
        "uptime_percent": Decimal("99.5"),
        "incidents": ("one delayed quote",),
        "proposed_config_improvements": ("tighten quote staleness",),
    }
    values.update(overrides)
    return OperationalSnapshot.model_validate(values)


def test_premarket_report_contains_the_complete_operational_snapshot() -> None:
    report = OperationalReportBuilder().premarket(_snapshot())

    assert report == (
        "# Pre-market report — DEMO/OFFLINE_SIM\n\n"
        "Generated UTC: 2026-09-03T13:45:00+00:00  \n"
        "Report version: operational-report-v1\n\n"
        "- Market regime: risk-on\n"
        "- System health: healthy\n"
        "- Overnight catalysts: cooler inflation print; ACME product launch\n"
        "- Federal/strategic changes: updated Fed guidance\n"
        "- Earnings risks: ACME after close\n"
        "- Watchlist: ACME; XYZ\n"
        "- Open positions/orders: 2/1\n"
    )


def test_end_of_day_report_contains_activity_pnl_and_control_evidence() -> None:
    report = OperationalReportBuilder().end_of_day(_snapshot())

    assert report == (
        "# End-of-day report — DEMO/OFFLINE_SIM\n\n"
        "Generated UTC: 2026-09-03T13:45:00+00:00  \n"
        "Report version: operational-report-v1\n\n"
        "- Candidates/rejections/proposals: 12/9/3\n"
        "- Realized/unrealized P&L: $12.34/$-5.67\n"
        "- Exact command intents / firewall denials: 3/2\n"
        "- MCP read/review errors: 1\n"
        "- Demo-vs-Live policy divergences: 2\n"
        "- Incidents: one delayed quote\n"
    )


def test_weekly_report_contains_qualification_reliability_and_improvements() -> None:
    report = OperationalReportBuilder().weekly(_snapshot())

    assert report == (
        "# Weekly report — DEMO/OFFLINE_SIM\n\n"
        "Generated UTC: 2026-09-03T13:45:00+00:00  \n"
        "Report version: operational-report-v1\n\n"
        "- Qualification regular sessions: 4/5\n"
        "- Operational uptime: 99.5%\n"
        "- Candidates/rejections/proposals: 12/9/3\n"
        "- Policy divergences: 2\n"
        "- Proposed configuration improvements: tighten quote staleness\n"
        "- Unresolved incidents: one delayed quote\n"
    )


@pytest.mark.parametrize("report_method", ["premarket", "end_of_day", "weekly"])
@pytest.mark.parametrize(
    ("environment", "demo_backend", "expected_label"),
    [
        (ExecutionEnvironment.LIVE, None, "LIVE"),
        (ExecutionEnvironment.DEMO, DemoBackend.OFFLINE_SIM, "DEMO/OFFLINE_SIM"),
        (ExecutionEnvironment.DEMO, DemoBackend.BROKER_SHADOW, "DEMO/BROKER_SHADOW"),
    ],
)
def test_every_report_variant_uses_an_unambiguous_environment_label(
    report_method: str,
    environment: ExecutionEnvironment,
    demo_backend: DemoBackend | None,
    expected_label: str,
) -> None:
    snapshot = _snapshot(environment=environment, demo_backend=demo_backend)

    report = getattr(OperationalReportBuilder(), report_method)(snapshot)

    assert report.splitlines()[0].endswith(f"— {expected_label}")


@pytest.mark.parametrize(
    "report_method",
    ["premarket", "end_of_day", "weekly"],
)
def test_reports_render_empty_operational_lists_explicitly(report_method: str) -> None:
    snapshot = _snapshot(
        overnight_catalysts=(),
        federal_changes=(),
        earnings_risks=(),
        watchlist=(),
        incidents=(),
        proposed_config_improvements=(),
    )

    report = getattr(OperationalReportBuilder(), report_method)(snapshot)

    assert ": none" in report


@pytest.mark.parametrize(
    "field",
    [
        "open_positions",
        "open_orders",
        "candidates_considered",
        "rejected_candidates",
        "proposals",
        "command_intents",
        "firewall_denials",
        "mcp_read_review_errors",
        "policy_divergences",
        "qualification_sessions",
    ],
)
def test_operational_snapshot_rejects_negative_counters(field: str) -> None:
    with pytest.raises(ValidationError):
        _snapshot(**{field: -1})


@pytest.mark.parametrize("uptime", [Decimal("-0.01"), Decimal("100.01")])
def test_operational_snapshot_rejects_out_of_range_uptime(uptime: Decimal) -> None:
    with pytest.raises(ValidationError):
        _snapshot(uptime_percent=uptime)


@pytest.mark.parametrize(
    "demo_backend",
    [DemoBackend.OFFLINE_SIM, DemoBackend.BROKER_SHADOW],
)
def test_operational_snapshot_rejects_live_environment_with_demo_backend(
    demo_backend: DemoBackend,
) -> None:
    with pytest.raises(ValidationError, match="LIVE operational state cannot have a Demo backend"):
        _snapshot(environment=ExecutionEnvironment.LIVE, demo_backend=demo_backend)


def test_operational_snapshot_rejects_demo_environment_without_backend() -> None:
    with pytest.raises(ValidationError, match="DEMO operational state requires a Demo backend"):
        _snapshot(environment=ExecutionEnvironment.DEMO, demo_backend=None)


def test_operational_snapshot_requires_an_aware_generation_timestamp() -> None:
    with pytest.raises(ValidationError, match="generated_at must be timezone-aware"):
        _snapshot(generated_at=datetime(2026, 9, 3, 13, 45))


def test_operational_snapshot_normalizes_generation_timestamp_to_utc() -> None:
    eastern = timezone(timedelta(hours=-4))

    snapshot = _snapshot(generated_at=datetime(2026, 9, 3, 9, 45, tzinfo=eastern))

    assert snapshot.generated_at == datetime(2026, 9, 3, 13, 45, tzinfo=UTC)
    assert snapshot.generated_at.tzinfo is UTC
    assert "Generated UTC: 2026-09-03T13:45:00+00:00" in OperationalReportBuilder().premarket(
        snapshot
    )


def test_weekly_report_does_not_append_a_percent_sign_to_unknown_uptime() -> None:
    report = OperationalReportBuilder().weekly(_snapshot(uptime_percent=None))

    assert "- Operational uptime: not measured\n" in report
    assert "not measured%" not in report
