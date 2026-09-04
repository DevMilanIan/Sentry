from __future__ import annotations

from collections.abc import Awaitable, Callable
from datetime import time
from decimal import Decimal
from zoneinfo import ZoneInfo

from app.api.dashboard import RuntimeView
from app.clock.base import Clock
from app.clock.market_calendar import UsEquityCalendar
from app.db.repository import InMemoryAuditRepository, PostgresAuditRepository
from app.notifications.provider import LocalNotificationProvider
from app.reporting.operational import OperationalReportBuilder, OperationalSnapshot


class RuntimeReporter:
    """Persist local operational reports and alerts; never changes trading state."""

    def __init__(
        self,
        view: RuntimeView,
        repository: InMemoryAuditRepository | PostgresAuditRepository,
        clock: Clock,
        snapshot: Callable[[], Awaitable[OperationalSnapshot]],
    ) -> None:
        self.view, self.repository, self.clock, self._snapshot = view, repository, clock, snapshot
        self.notifications = LocalNotificationProvider(
            clock, view.binding.environment, view.binding.demo_backend, repository.append
        )
        self.builder = OperationalReportBuilder()
        self.calendar = UsEquityCalendar()

    async def tick(self) -> None:
        state = self.view.safety.state.value
        prior = await self.repository.find_payload(
            "system_runs", "record_kind", "last_notified_safety_state"
        )
        if prior is None or prior["payload"]["safety_state"] != state:
            await self.notifications.send(
                "runtime_safety",
                4 if state == "HALTED" else 2,
                f"Runtime safety: {state}",
                self.view.safety.reason,
            )
            await self.repository.append(
                "system_runs",
                {
                    "created_at": self.clock.now(),
                    "environment": self.view.binding.environment.value,
                    "record_kind": "last_notified_safety_state",
                    "safety_state": state,
                },
            )
        local = self.clock.now().astimezone(ZoneInfo("America/New_York"))
        replay = bool(self.view.replay)
        report_type: str | None = None
        if replay and self.view.replay.get("complete"):
            report_type = "replay_completion"
        elif not replay:
            # Raises explicitly when the official schedule needs a refresh;
            # an unverified year must not silently produce a guessed EOD report.
            close = self.calendar.regular_session_close(local.date())
            if close is not None and time(8) <= local.time() < time(9, 30):
                report_type = "premarket"
            elif close is not None and local >= close:
                report_type = "weekly" if local.weekday() == 4 else "end_of_day"
        if report_type is None:
            return
        key = f"{self.view.binding.idempotency_namespace}:{local.date()}:{report_type}"
        if replay:
            key += f":{self.view.replay['fixture_hash']}"
        if await self.repository.find_payload("system_runs", "report_key", key) is not None:
            return
        snapshot = await self._snapshot()
        if report_type == "premarket":
            body = self.builder.premarket(snapshot)
        elif report_type == "weekly":
            body = self.builder.weekly(snapshot)
        else:
            body = self.builder.end_of_day(snapshot)
        if replay:
            body = (
                "Replay completion — finite fixture, not a real-market qualification session.\n\n"
                + body
            )
        await self.repository.append(
            "system_runs",
            {
                "created_at": self.clock.now(),
                "environment": self.view.binding.environment.value,
                "record_kind": "operational_report",
                "report_key": key,
                "report_type": report_type,
                "data_mode": "REPLAY"
                if replay
                else ("LIVE_READS" if self.view.broker_connected else "UNAVAILABLE"),
                "count_scope": "current namespace (bounded at 10000 rows per category)",
                "snapshot": snapshot.model_dump(mode="json"),
                "markdown": body,
            },
        )


async def snapshot_from_view(
    view: RuntimeView,
    repository: InMemoryAuditRepository | PostgresAuditRepository,
    clock: Clock,
    *,
    realized_pnl: Decimal = Decimal("0"),
) -> OperationalSnapshot:
    counts: dict[str, int] = {}
    for table in (
        "candidate_packets",
        "trade_proposals",
        "broker_command_intents",
        "external_write_firewall_events",
        "rejected_candidate_outcomes",
    ):
        rows = await repository.list_payloads(table, limit=10_000)
        if len(rows) == 10_000:
            raise RuntimeError("report namespace requires pagination; refusing truncated totals")
        counts[table] = len(rows)
    return OperationalSnapshot(
        generated_at=clock.now(),
        environment=view.binding.environment,
        demo_backend=view.binding.demo_backend,
        market_regime="historical replay" if view.replay else "not yet classified",
        system_health=f"{view.safety.state.value}: {view.safety.reason}",
        open_positions=len(view.positions),
        open_orders=len(view.open_orders),
        candidates_considered=counts["candidate_packets"],
        rejected_candidates=counts["rejected_candidate_outcomes"],
        proposals=counts["trade_proposals"],
        command_intents=counts["broker_command_intents"],
        firewall_denials=counts["external_write_firewall_events"],
        realized_pnl=realized_pnl,
        incidents=tuple(view.recent_errors),
    )
