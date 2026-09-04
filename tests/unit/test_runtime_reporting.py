from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from app.api.dashboard import RuntimeView
from app.clock.base import VirtualClock
from app.clock.market_calendar import CalendarCoverageError
from app.config import RuntimeBinding
from app.db.repository import InMemoryAuditRepository
from app.domain.enums import TradingMode
from app.reporting.operational import OperationalSnapshot
from app.reporting.runtime import RuntimeReporter, snapshot_from_view
from app.safety.runtime_state import SafetyController


async def test_replay_completion_report_and_alert_survive_reporter_restart(
    demo_binding: RuntimeBinding,
    instant: datetime,
) -> None:
    clock = VirtualClock(instant)
    repo = InMemoryAuditRepository(demo_binding)
    view = RuntimeView(demo_binding, TradingMode.RESEARCH, SafetyController(clock, timedelta(0)))
    view.replay = {"complete": True, "fixture_hash": "fixture-v1"}

    async def snapshot() -> OperationalSnapshot:
        return await snapshot_from_view(view, repo, clock, realized_pnl=Decimal("6"))

    await RuntimeReporter(view, repo, clock, snapshot).tick()
    await RuntimeReporter(view, repo, clock, snapshot).tick()
    assert len(await repo.list("notification_events")) == 1
    reports = await repo.list_payloads("system_runs", filters={"record_kind": "operational_report"})
    assert len(reports) == 1
    assert reports[0]["payload"]["data_mode"] == "REPLAY"
    assert "not a real-market qualification session" in reports[0]["payload"]["markdown"]
    assert reports[0]["payload"]["snapshot"]["realized_pnl"] == "6"
    view.safety.emergency_stop("test incident")
    await RuntimeReporter(view, repo, clock, snapshot).tick()
    assert len(await repo.list("notification_events")) == 2


@pytest.mark.parametrize(
    "close,report_type",
    [
        (datetime(2026, 11, 27, 18, tzinfo=UTC), "weekly"),
        (datetime(2026, 12, 24, 18, tzinfo=UTC), "end_of_day"),
        (datetime(2026, 9, 3, 20, tzinfo=UTC), "end_of_day"),
    ],
)
async def test_reporting_uses_scheduled_close_including_half_days(
    demo_binding: RuntimeBinding,
    close: datetime,
    report_type: str,
) -> None:
    clock = VirtualClock(close - timedelta(seconds=1))
    repo = InMemoryAuditRepository(demo_binding)
    view = RuntimeView(demo_binding, TradingMode.RESEARCH, SafetyController(clock, timedelta(0)))

    async def snapshot() -> OperationalSnapshot:
        return await snapshot_from_view(view, repo, clock)

    reporter = RuntimeReporter(view, repo, clock, snapshot)
    await reporter.tick()
    assert not await repo.list_payloads(
        "system_runs", filters={"record_kind": "operational_report"}
    )
    await clock.advance_to(close)
    await reporter.tick()
    reports = await repo.list_payloads("system_runs", filters={"record_kind": "operational_report"})
    assert len(reports) == 1
    assert reports[0]["payload"]["report_type"] == report_type


async def test_reporting_cannot_guess_a_future_unverified_session_close(
    demo_binding: RuntimeBinding,
) -> None:
    clock = VirtualClock(datetime(2029, 1, 2, 21, tzinfo=UTC))
    repo = InMemoryAuditRepository(demo_binding)
    view = RuntimeView(demo_binding, TradingMode.RESEARCH, SafetyController(clock, timedelta(0)))

    async def snapshot() -> OperationalSnapshot:
        return await snapshot_from_view(view, repo, clock)

    with pytest.raises(CalendarCoverageError, match="unverified"):
        await RuntimeReporter(view, repo, clock, snapshot).tick()
    assert not await repo.list_payloads(
        "system_runs", filters={"record_kind": "operational_report"}
    )
