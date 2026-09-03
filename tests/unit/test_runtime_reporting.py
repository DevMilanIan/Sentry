from __future__ import annotations

from datetime import datetime, timedelta
from decimal import Decimal

from app.api.dashboard import RuntimeView
from app.clock.base import VirtualClock
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
