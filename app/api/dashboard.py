from __future__ import annotations

import hmac
import os
from collections import deque
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import timedelta
from decimal import Decimal
from typing import Any, Protocol
from uuid import UUID

from fastapi import Depends, FastAPI, Header, HTTPException, Response, status
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, ConfigDict, Field

from app.clock.base import Clock
from app.config import AppConfig, RuntimeBinding
from app.domain.enums import ExecutionEnvironment, RuntimeSafetyState, TradingMode
from app.domain.models import AccountSnapshot, ExactApproval, TradeProposal
from app.observability.metrics import MetricsRegistry
from app.safety.runtime_state import SafetyController, SafetyEvidence


class AuditRepository(Protocol):
    async def append(self, table: str, value: BaseModel | dict[str, Any]) -> UUID: ...

    async def list(self, table: str, *, limit: int = 100) -> list[dict[str, Any]]: ...

    async def healthcheck(self) -> bool: ...


@dataclass(slots=True)
class RuntimeView:
    binding: RuntimeBinding
    trading_mode: TradingMode
    safety: SafetyController
    broker_connected: bool = False
    model_healthy: bool = False
    database_healthy: bool = False
    market_data_fresh: bool = False
    reconciled: bool = False
    execution_service_healthy: bool = False
    unresolved_submission: bool = True
    replay: dict[str, Any] = field(default_factory=dict)
    write_firewall: str = "NOT_APPLICABLE"
    last_scan_at: str | None = None
    observed_broker_account: AccountSnapshot | None = None
    effective_account: AccountSnapshot | None = None
    candidates: list[dict[str, Any]] = field(default_factory=list)
    proposals: dict[UUID, TradeProposal] = field(default_factory=dict)
    open_orders: list[dict[str, Any]] = field(default_factory=list)
    positions: list[dict[str, Any]] = field(default_factory=list)
    recent_errors: deque[str] = field(default_factory=lambda: deque(maxlen=100))
    qualification: dict[str, Any] = field(
        default_factory=lambda: {"status": "NOT_STARTED", "sessions": 0}
    )
    last_safety_evidence: SafetyEvidence | None = None

    def snapshot(self) -> dict[str, Any]:
        return {
            "execution_environment": self.binding.environment.value,
            "demo_backend": self.binding.demo_backend.value if self.binding.demo_backend else None,
            "trading_mode": self.trading_mode.value,
            "runtime_safety_state": self.safety.state.value,
            "safety_reason": self.safety.reason,
            "broker_connected": self.broker_connected,
            "model_healthy": self.model_healthy,
            "database_healthy": self.database_healthy,
            "market_data_fresh": self.market_data_fresh,
            "reconciled": self.reconciled,
            "execution_service_healthy": self.execution_service_healthy,
            "unresolved_submission": self.unresolved_submission,
            "replay": self.replay,
            "external_write_firewall": self.write_firewall,
            "last_scan_at": self.last_scan_at,
            "observed_broker_account": _public_account_snapshot(self.observed_broker_account)
            if self.observed_broker_account
            else None,
            "effective_account": _public_account_snapshot(self.effective_account)
            if self.effective_account
            else None,
            "candidates": self.candidates[:50],
            "proposals": [proposal.model_dump(mode="json") for proposal in self.proposals.values()],
            "open_orders": self.open_orders[:50],
            "positions": self.positions[:50],
            "recent_errors": list(self.recent_errors)[-20:],
            "qualification": self.qualification,
        }


class ApprovalRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    maximum_limit_price: Decimal = Field(gt=0)
    expires_in_seconds: int = Field(default=300, ge=10, le=1800)
    approved_by: str = Field(default="local-dashboard", min_length=1, max_length=80)


class ModeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    mode: TradingMode


def _public_account_snapshot(snapshot: AccountSnapshot) -> dict[str, Any]:
    payload = snapshot.model_dump(mode="json", exclude={"account_fingerprint"})
    payload["account_fingerprint_present"] = snapshot.account_fingerprint is not None
    return payload


def _dashboard_html() -> str:
    return """<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Options Sentinel</title><style>
:root{color-scheme:dark;--bg:#0b1220;--panel:#121d30;--line:#26354e;--text:#edf4ff;--muted:#9fb0c8;--ok:#4dd4a7;--bad:#ff6b7a;--warn:#ffc857}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--text);font:15px system-ui,sans-serif}header{position:sticky;top:0;padding:16px 24px;background:#08101de8;border-bottom:1px solid var(--line);backdrop-filter:blur(8px)}h1{font-size:20px;margin:0}.labels{display:flex;gap:8px;flex-wrap:wrap;margin-top:8px}.pill{padding:4px 9px;border:1px solid var(--line);border-radius:99px;font-weight:700}.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(300px,1fr));gap:14px;padding:18px}.panel{background:var(--panel);border:1px solid var(--line);border-radius:12px;padding:16px;min-height:150px}.panel h2{font-size:16px;margin:0 0 12px}.row{display:flex;justify-content:space-between;gap:16px;padding:6px 0;border-bottom:1px solid #1d2b40}.row span:first-child{color:var(--muted)}button{background:#243b5e;color:var(--text);border:1px solid #42618e;border-radius:8px;padding:8px 10px;margin:4px;cursor:pointer}button.danger{background:#5f2330;border-color:#a74455}pre{white-space:pre-wrap;word-break:break-word;color:var(--muted);max-height:280px;overflow:auto}.bad{color:var(--bad)}.ok{color:var(--ok)}.warn{color:var(--warn)}</style></head>
<body><header><h1>Options Sentinel</h1><div class="labels"><span class="pill" id="env">LOADING</span><span class="pill" id="backend"></span><span class="pill" id="mode"></span><span class="pill" id="safety"></span></div></header>
<main class="grid">
<section class="panel"><h2>System</h2><div id="system"></div></section>
<section class="panel"><h2>Account separation</h2><div id="accounts"></div></section>
<section class="panel"><h2>Candidates</h2><pre id="candidates">[]</pre></section>
<section class="panel"><h2>Proposals</h2><pre id="proposals">[]</pre></section>
<section class="panel"><h2>Open orders</h2><pre id="open_orders">[]</pre></section>
<section class="panel"><h2>Positions</h2><pre id="positions">[]</pre></section>
<section class="panel"><h2>Broker command intents</h2><pre id="intents">[]</pre></section>
<section class="panel"><h2>Qualification</h2><pre id="qualification">{}</pre></section>
<section class="panel"><h2>Operations</h2><input id="token" type="password" placeholder="local control token"><br><button onclick="act('/api/control/pause')">Pause entries</button><button onclick="act('/api/control/reconcile')">Reconcile</button><button class="danger" onclick="act('/api/control/emergency-stop')">Emergency stop</button><pre id="action"></pre></section>
<section class="panel"><h2>Recent errors</h2><pre id="errors">[]</pre></section>
</main><script>
const esc=v=>String(v??'unknown').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const row=(k,v)=>`<div class="row"><span>${esc(k)}</span><strong>${esc(v)}</strong></div>`;
async function refresh(){let s=await (await fetch('/api/state')).json(); document.querySelector('#env').textContent=s.execution_environment;document.querySelector('#backend').textContent=s.demo_backend||'LIVE BROKER';document.querySelector('#mode').textContent=s.trading_mode;document.querySelector('#safety').textContent=s.runtime_safety_state;document.querySelector('#system').innerHTML=row('Safety reason',s.safety_reason)+row('Broker',s.broker_connected)+row('Model',s.model_healthy)+row('Database',s.database_healthy)+row('Market data fresh',s.market_data_fresh)+row('Reconciled',s.reconciled)+row('Write firewall',s.external_write_firewall)+row('Last scan',s.last_scan_at);let observed=s.observed_broker_account?`${s.observed_broker_account.cash} cash / ${s.observed_broker_account.buying_power} BP`:'not connected';let effective=s.effective_account?`${s.effective_account.cash} cash / ${s.effective_account.buying_power} BP`:'not initialized';document.querySelector('#accounts').innerHTML=row('REAL BROKER OBSERVED',observed)+row('EFFECTIVE/SHADOW EXECUTION',effective);for(let id of ['candidates','proposals','open_orders','positions','qualification','errors'])document.querySelector('#'+id).textContent=JSON.stringify(s[id]??s.recent_errors,null,2);let i=await (await fetch('/api/broker-command-intents')).json();document.querySelector('#intents').textContent=JSON.stringify(i,null,2)}
async function act(path){let t=document.querySelector('#token').value;let r=await fetch(path,{method:'POST',headers:{'X-Dashboard-Token':t}});document.querySelector('#action').textContent=await r.text();await refresh()}refresh();setInterval(refresh,5000);
</script></body></html>"""


def create_app(
    config: AppConfig,
    view: RuntimeView,
    repository: AuditRepository,
    clock: Clock,
    metrics: MetricsRegistry,
    *,
    dashboard_token: str | None = None,
    reconcile: Callable[[], Awaitable[bool]] | None = None,
) -> FastAPI:
    application = FastAPI(title="Options Sentinel", version="0.1.0", docs_url="/api/docs")
    expected_token = dashboard_token or os.getenv("SENTRY_DASHBOARD_TOKEN", "")

    async def authorize_control(x_dashboard_token: str = Header(default="")) -> None:
        if not config.dashboard.require_token_for_controls:
            return
        if not expected_token or not hmac.compare_digest(expected_token, x_dashboard_token):
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED, detail="invalid local control token"
            )

    async def audit(action: str, result: str) -> None:
        await repository.append(
            "environment_audit_events",
            {
                "created_at": clock.now(),
                "environment": view.binding.environment.value,
                "action": action,
                "result": result,
                "actor": "local-dashboard",
            },
        )

    @application.get("/", response_class=HTMLResponse)
    async def dashboard() -> str:
        return _dashboard_html()

    @application.get("/health")
    async def health(response: Response) -> dict[str, Any]:
        view.database_healthy = await repository.healthcheck()
        healthy = view.database_healthy and view.safety.state is not RuntimeSafetyState.HALTED
        if not healthy:
            response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
        return {"healthy": healthy, **view.snapshot()}

    @application.get("/metrics")
    async def prometheus_metrics() -> Response:
        return Response(await metrics.render_prometheus(), media_type="text/plain; version=0.0.4")

    @application.get("/api/state")
    async def state_snapshot() -> dict[str, Any]:
        return view.snapshot()

    @application.get("/api/broker-command-intents")
    async def broker_command_intents() -> list[dict[str, Any]]:
        return await repository.list("broker_command_intents", limit=50)

    @application.get("/api/notifications")
    async def notifications() -> list[dict[str, Any]]:
        return await repository.list("notification_events", limit=50)

    @application.get("/api/reports")
    async def reports() -> list[dict[str, Any]]:
        return [row for row in await repository.list("system_runs", limit=500)
                if row["payload"].get("record_kind") == "operational_report"][-20:]

    @application.post("/api/control/emergency-stop", dependencies=[Depends(authorize_control)])
    async def emergency_stop() -> dict[str, str]:
        disabled_file = config.runtime.disabled_file
        disabled_file.parent.mkdir(parents=True, exist_ok=True)
        disabled_file.write_text("disabled by local dashboard\n", encoding="utf-8")
        view.safety.emergency_stop("local dashboard emergency stop")
        await audit("EMERGENCY_STOP", "HALTED")
        return {"state": view.safety.state.value}

    @application.post("/api/control/pause", dependencies=[Depends(authorize_control)])
    async def pause_entries() -> dict[str, str]:
        await audit("PAUSE_ENTRIES", "REQUESTED")
        view.safety.pause_entries("paused by local dashboard")
        return {"state": view.safety.state.value}

    @application.post("/api/control/resume", dependencies=[Depends(authorize_control)])
    async def resume_entries() -> dict[str, str]:
        if view.last_safety_evidence is None:
            raise HTTPException(status_code=409, detail="fresh health evidence is unavailable")
        await audit("REQUEST_RESUME", "REQUESTED")
        view.safety.resume_entries(view.last_safety_evidence, manual_halt_cleared=False)
        return {"state": view.safety.state.value, "reason": view.safety.reason}

    @application.post("/api/control/reconcile", dependencies=[Depends(authorize_control)])
    async def force_reconcile() -> dict[str, Any]:
        if reconcile is None:
            raise HTTPException(status_code=503, detail="reconciliation service unavailable")
        await audit("FORCE_RECONCILE", "REQUESTED")
        success = await reconcile()
        view.reconciled = success
        if not success:
            view.safety.degrade(RuntimeSafetyState.ENTRY_DISABLED, "manual reconciliation failed")
        await audit("FORCE_RECONCILE_RESULT", "PASS" if success else "FAIL")
        return {"reconciled": success, "state": view.safety.state.value}

    @application.post("/api/control/mode", dependencies=[Depends(authorize_control)])
    async def set_mode(request: ModeRequest) -> dict[str, str]:
        if view.binding.environment is ExecutionEnvironment.LIVE and request.mode in {
            TradingMode.SHADOW,
            TradingMode.EXIT_AUTO,
            TradingMode.AUTO,
        }:
            raise HTTPException(
                status_code=409, detail="requested Live mode requires a separate activation gate"
            )
        if (
            request.mode is not TradingMode.RESEARCH
            and view.safety.state is RuntimeSafetyState.HALTED
        ):
            raise HTTPException(status_code=409, detail="HALTED permits RESEARCH only")
        previous = view.trading_mode
        await audit("SET_TRADING_MODE", f"{previous.value}->{request.mode.value}")
        view.trading_mode = request.mode
        return {"mode": view.trading_mode.value}

    @application.post(
        "/api/proposals/{proposal_id}/approve", dependencies=[Depends(authorize_control)]
    )
    async def approve_proposal(proposal_id: UUID, request: ApprovalRequest) -> dict[str, Any]:
        proposal = view.proposals.get(proposal_id)
        if proposal is None:
            raise HTTPException(status_code=404, detail="proposal not found")
        if view.trading_mode not in {TradingMode.APPROVAL, TradingMode.EXIT_AUTO}:
            raise HTTPException(
                status_code=409, detail="current mode does not accept entry approvals"
            )
        if request.maximum_limit_price < proposal.limit_price:
            raise HTTPException(status_code=422, detail="approval bound is below proposal limit")
        approval = ExactApproval(
            created_at=clock.now(),
            environment=proposal.environment,
            namespace=proposal.namespace,
            proposal_id=proposal.proposal_id,
            order_fingerprint=proposal.order_fingerprint,
            maximum_limit_price=request.maximum_limit_price,
            expires_at=clock.now() + timedelta(seconds=request.expires_in_seconds),
            approved_by=request.approved_by,
        )
        await repository.append("approvals", approval)
        await audit("APPROVE_EXACT_PROPOSAL", str(proposal_id))
        return approval.model_dump(mode="json")

    @application.post(
        "/api/proposals/{proposal_id}/reject", dependencies=[Depends(authorize_control)]
    )
    async def reject_proposal(proposal_id: UUID) -> dict[str, str]:
        proposal = view.proposals.get(proposal_id)
        if proposal is None:
            raise HTTPException(status_code=404, detail="proposal not found")
        await repository.append(
            "approvals",
            ExactApproval(
                created_at=clock.now(),
                environment=proposal.environment,
                namespace=proposal.namespace,
                proposal_id=proposal.proposal_id,
                order_fingerprint=proposal.order_fingerprint,
                maximum_limit_price=proposal.limit_price,
                expires_at=clock.now(),
                approved_by="local-dashboard",
                rejected=True,
            ),
        )
        await audit("REJECT_PROPOSAL", str(proposal_id))
        view.proposals.pop(proposal_id, None)
        return {"proposal_id": str(proposal_id), "status": "REJECTED"}

    return application
