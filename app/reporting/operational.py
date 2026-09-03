from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

from pydantic import Field, field_validator, model_validator

from app.domain.enums import DemoBackend, ExecutionEnvironment
from app.domain.models import DomainModel


class OperationalSnapshot(DomainModel):
    generated_at: datetime
    environment: ExecutionEnvironment
    demo_backend: DemoBackend | None
    market_regime: str
    system_health: str
    overnight_catalysts: tuple[str, ...] = ()
    federal_changes: tuple[str, ...] = ()
    earnings_risks: tuple[str, ...] = ()
    watchlist: tuple[str, ...] = ()
    open_positions: int = Field(ge=0)
    open_orders: int = Field(ge=0)
    candidates_considered: int = Field(ge=0)
    rejected_candidates: int = Field(ge=0)
    proposals: int = Field(ge=0)
    realized_pnl: Decimal = Decimal("0")
    unrealized_pnl: Decimal = Decimal("0")
    command_intents: int = Field(default=0, ge=0)
    firewall_denials: int = Field(default=0, ge=0)
    mcp_read_review_errors: int = Field(default=0, ge=0)
    policy_divergences: int = Field(default=0, ge=0)
    qualification_sessions: int = Field(default=0, ge=0)
    uptime_percent: Decimal | None = Field(default=None, ge=0, le=100)
    incidents: tuple[str, ...] = ()
    proposed_config_improvements: tuple[str, ...] = ()

    @field_validator("generated_at")
    @classmethod
    def generated_at_is_utc(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("generated_at must be timezone-aware")
        return value.astimezone(UTC)

    @model_validator(mode="after")
    def backend_matches_environment(self) -> OperationalSnapshot:
        if self.environment is ExecutionEnvironment.LIVE and self.demo_backend is not None:
            raise ValueError("LIVE operational state cannot have a Demo backend")
        if self.environment is ExecutionEnvironment.DEMO and self.demo_backend is None:
            raise ValueError("DEMO operational state requires a Demo backend")
        return self

    @property
    def label(self) -> str:
        if self.demo_backend:
            return f"{self.environment.value}/{self.demo_backend.value}"
        return self.environment.value


class OperationalReportBuilder:
    version = "operational-report-v1"

    def premarket(self, snapshot: OperationalSnapshot) -> str:
        return (
            self._header("Pre-market report", snapshot)
            + "\n".join(
                (
                    f"- Market regime: {snapshot.market_regime}",
                    f"- System health: {snapshot.system_health}",
                    f"- Overnight catalysts: {self._join(snapshot.overnight_catalysts)}",
                    f"- Federal/strategic changes: {self._join(snapshot.federal_changes)}",
                    f"- Earnings risks: {self._join(snapshot.earnings_risks)}",
                    f"- Watchlist: {self._join(snapshot.watchlist)}",
                    f"- Open positions/orders: {snapshot.open_positions}/{snapshot.open_orders}",
                )
            )
            + "\n"
        )

    def end_of_day(self, snapshot: OperationalSnapshot) -> str:
        activity = (
            f"{snapshot.candidates_considered}/{snapshot.rejected_candidates}/{snapshot.proposals}"
        )
        pnl = f"${snapshot.realized_pnl}/${snapshot.unrealized_pnl}"
        command_evidence = f"{snapshot.command_intents}/{snapshot.firewall_denials}"
        return (
            self._header("End-of-day report", snapshot)
            + "\n".join(
                (
                    f"- Candidates/rejections/proposals: {activity}",
                    f"- Realized/unrealized P&L: {pnl}",
                    f"- Exact command intents / firewall denials: {command_evidence}",
                    f"- MCP read/review errors: {snapshot.mcp_read_review_errors}",
                    f"- Demo-vs-Live policy divergences: {snapshot.policy_divergences}",
                    f"- Incidents: {self._join(snapshot.incidents)}",
                )
            )
            + "\n"
        )

    def weekly(self, snapshot: OperationalSnapshot) -> str:
        uptime = (
            f"{snapshot.uptime_percent}%"
            if snapshot.uptime_percent is not None
            else "not measured"
        )
        activity = (
            f"{snapshot.candidates_considered}/{snapshot.rejected_candidates}/{snapshot.proposals}"
        )
        improvements = self._join(snapshot.proposed_config_improvements)
        return (
            self._header("Weekly report", snapshot)
            + "\n".join(
                (
                    f"- Qualification regular sessions: {snapshot.qualification_sessions}/5",
                    f"- Operational uptime: {uptime}",
                    f"- Candidates/rejections/proposals: {activity}",
                    f"- Policy divergences: {snapshot.policy_divergences}",
                    f"- Proposed configuration improvements: {improvements}",
                    f"- Unresolved incidents: {self._join(snapshot.incidents)}",
                )
            )
            + "\n"
        )

    def _header(self, title: str, snapshot: OperationalSnapshot) -> str:
        generated = snapshot.generated_at.isoformat()
        return (
            f"# {title} — {snapshot.label}\n\n"
            f"Generated UTC: {generated}  \n"
            f"Report version: {self.version}\n\n"
        )

    @staticmethod
    def _join(values: tuple[str, ...]) -> str:
        return "; ".join(values) if values else "none"
