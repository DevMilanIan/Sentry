from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import timedelta

from fastapi import FastAPI

from app.api.dashboard import RuntimeView, create_app
from app.broker.base import BrokerAccountExecution
from app.catalysts.collector import OfficialSourceCollector
from app.catalysts.runtime import CatalystIngestionWorker
from app.clock.base import Clock, RealClock
from app.config import LoadedConfig
from app.controller.master import MasterController, PeriodicJob
from app.db.repository import InMemoryAuditRepository, PostgresAuditRepository
from app.db.session import DatabaseManager
from app.demo.runtime import OfflineRuntime
from app.domain.enums import DemoBackend, ExecutionEnvironment
from app.learning.outcomes import ClosedPositionReviewWorker
from app.market.base import MarketDataProvider
from app.market.models import ReplayFixture
from app.observability.metrics import MetricsRegistry
from app.reasoning.ollama import OllamaModelProvider
from app.reasoning.provider import LocalModelProvider
from app.reporting.operational import OperationalSnapshot
from app.reporting.runtime import RuntimeReporter, snapshot_from_view
from app.safety.runtime_state import SafetyController
from app.sentinel.live_reads import LiveReadSurveillanceWorker
from app.strategy.live_research import LiveMarketResearchQueue
from app.strategy.runtime import CandidateResearchWorker


@dataclass(slots=True)
class ApplicationRuntime:
    loaded: LoadedConfig
    database: DatabaseManager | None
    repository: InMemoryAuditRepository | PostgresAuditRepository
    view: RuntimeView
    controller: MasterController
    broker: BrokerAccountExecution | None
    model_provider: LocalModelProvider
    application: FastAPI
    offline: OfflineRuntime | None = None
    market_provider: MarketDataProvider | None = None
    _closed: bool = False

    async def close(self) -> None:
        if self._closed:
            return
        # An incomplete stop retains the instance lock and clients until the
        # in-flight callbacks finish. Allow a later close attempt to retry it.
        await self.controller.stop()
        try:
            await self.model_provider.close()
        finally:
            try:
                if self.market_provider is not None:
                    await self.market_provider.close()
            finally:
                if self.database is not None:
                    await self.database.close()
        self._closed = True


async def build_application(
    loaded: LoadedConfig,
    *,
    dashboard_token: str | None = None,
    repository: InMemoryAuditRepository | PostgresAuditRepository | None = None,
    model_provider: LocalModelProvider | None = None,
    wall_clock: Clock | None = None,
    fixture: ReplayFixture | None = None,
    market_provider: MarketDataProvider | None = None,
    market_watchlist: tuple[str, ...] = (),
) -> ApplicationRuntime:
    """Compose production services; in-memory persistence is explicit test injection only."""
    binding = loaded.bind_runtime()
    if market_provider is not None and binding.demo_backend is not DemoBackend.BROKER_SHADOW:
        raise ValueError("current-data injection requires DEMO/BROKER_SHADOW")
    if bool(market_watchlist) != (market_provider is not None):
        raise ValueError("current-data injection requires both a provider and explicit watchlist")
    clock = wall_clock or RealClock()
    safety = SafetyController(
        clock, timedelta(seconds=loaded.app.runtime.startup_health_window_seconds)
    )
    if binding.environment is ExecutionEnvironment.LIVE:
        safety.emergency_stop("LIVE startup requires explicit staged activation")
    database: DatabaseManager | None = None
    if repository is None:
        database = DatabaseManager(
            loaded.app.database.url, binding, shared_schema=loaded.app.database.shared_schema
        )
        repository = PostgresAuditRepository(database)
    elif repository.binding != binding:
        raise ValueError("injected repository must match immutable runtime binding")
    audit = repository
    metrics = MetricsRegistry()
    provider = model_provider or OllamaModelProvider(loaded.model_provider)
    firewall = (
        "LOCAL_SIMULATION_ONLY"
        if binding.demo_backend is DemoBackend.OFFLINE_SIM
        else "DENY_ALL_WRITES"
    )
    view = RuntimeView(
        binding=binding,
        trading_mode=loaded.app.trading_mode,
        safety=safety,
        write_firewall=firewall,
    )
    offline: OfflineRuntime | None = None
    broker: BrokerAccountExecution | None = None
    surveillance: LiveReadSurveillanceWorker | None = None
    research_queue: LiveMarketResearchQueue | None = None
    try:
        if market_provider is not None:
            surveillance = LiveReadSurveillanceWorker(
                market_provider, clock, audit, watchlist=market_watchlist
            )
            researcher = CandidateResearchWorker(
                loaded,
                clock,
                audit,
                provider,
                policy_profile=loaded.decision_policies.profiles["DEMO_EXPLORATORY"],
            )
            research_queue = LiveMarketResearchQueue(audit, clock, market_provider, researcher)
        if binding.demo_backend is DemoBackend.OFFLINE_SIM:
            if fixture is None:
                raw = await asyncio.to_thread(
                    loaded.app.runtime.offline_fixture.read_text, encoding="utf-8"
                )
                fixture = ReplayFixture.model_validate_json(raw)
            offline = await OfflineRuntime.create(loaded, audit, view, clock, fixture)
            offline.candidate_worker = CandidateResearchWorker(
                loaded,
                offline.clock,
                audit,
                provider,
                policy_profile=loaded.decision_policies.profiles["DEMO_EXPLORATORY"],
            )
            broker = offline.broker
    except BaseException:
        try:
            await provider.close()
        finally:
            try:
                if market_provider is not None:
                    await market_provider.close()
            finally:
                if database is not None:
                    await database.close()
        raise

    async def broker_health() -> bool:
        if surveillance is not None:
            # Re-evaluate age on every health tick, not only the five-minute scan.
            view.market_data_fresh = await surveillance.health()
        if broker is None:
            return False
        report = await broker.reconcile()
        view.observed_broker_account = report.observed_account
        view.effective_account = report.effective_account
        return report.successful

    async def reconcile() -> bool:
        if offline is not None:
            return await offline.reconcile()
        return False  # Authenticated adapters cannot be inferred from config alone.

    async def execution_health() -> bool:
        if offline is not None:
            return await offline.health()
        view.unresolved_submission = True
        return False

    controller = MasterController(
        loaded.app,
        view,
        clock,
        metrics,
        repository_health=audit.healthcheck,
        broker_health=broker_health,
        reconcile=reconcile,
        execution_health=execution_health,
    )

    async def model_health() -> None:
        health = await provider.health()
        view.model_healthy = health.healthy
        await audit.append(
            "health_events",
            {
                "created_at": clock.now(),
                "environment": binding.environment.value,
                "component": "local_model",
                **health.model_dump(mode="json"),
            },
        )

    async def monitor_account_state() -> None:
        if broker is None:
            view.broker_connected = False
            return
        observed = await broker.get_observed_broker_account_state()
        effective = await broker.get_effective_execution_account_state()
        positions = await broker.get_positions()
        orders = await broker.get_orders()
        view.observed_broker_account, view.effective_account = observed, effective
        view.positions = [item.model_dump(mode="json") for item in positions]
        view.open_orders = [
            item.model_dump(mode="json")
            for item in orders
            if item.state.value in {"OPEN", "PARTIAL", "SUBMISSION_UNKNOWN", "SUBMITTING"}
        ]
        await audit.append("broker_observed_account_snapshots", observed)
        await audit.append(
            "position_snapshots",
            {
                "created_at": clock.now(),
                "environment": binding.environment.value,
                "demo_backend": binding.demo_backend.value if binding.demo_backend else None,
                "effective_account": effective.model_dump(mode="json"),
                "positions": view.positions,
                "open_orders": view.open_orders,
            },
        )

    controller.add_job(
        PeriodicJob("model_health", loaded.app.sentinel.health_seconds, model_health)
    )
    controller.add_job(
        PeriodicJob("account_monitor", loaded.app.sentinel.positions_seconds, monitor_account_state)
    )
    if offline is not None:
        controller.add_job(
            PeriodicJob("offline_replay", loaded.app.runtime.offline_step_seconds, offline.step)
        )
        controller.add_job(
            PeriodicJob(
                "proposal_dispatch",
                loaded.app.runtime.offline_step_seconds,
                offline.dispatch_proposals,
            )
        )
    else:
        source_worker = CatalystIngestionWorker(
            loaded.sources,
            clock,
            audit,
            OfficialSourceCollector(clock, user_agent=loaded.sources.sec_user_agent),
        )
        controller.add_job(
            PeriodicJob("official_sources", loaded.sources.poll_seconds, source_worker.poll)
        )
        if surveillance is not None:

            async def scan_current_market() -> None:
                assert surveillance is not None
                try:
                    await surveillance.tick()
                    view.market_data_fresh = await surveillance.health()
                    view.last_scan_at = clock.now().isoformat()
                except BaseException:
                    view.market_data_fresh = False
                    raise

            controller.add_job(
                PeriodicJob(
                    "current_market_surveillance",
                    loaded.app.sentinel.equity_scan_seconds,
                    scan_current_market,
                    timeout_seconds=45,
                )
            )

            async def research_pending_market_event() -> None:
                assert research_queue is not None
                proposal = await research_queue.tick()
                if proposal is not None:
                    # Proposal visibility is not execution authority. The credentialed
                    # broker/execution composition remains absent and fail-closed.
                    view.proposals[proposal.proposal_id] = proposal

            controller.add_job(
                PeriodicJob(
                    "market_event_research", 5, research_pending_market_event, timeout_seconds=210
                )
            )
    trading_clock = offline.clock if offline is not None else clock
    outcome_worker = ClosedPositionReviewWorker(audit, trading_clock)

    async def review_closed_positions() -> None:
        if offline is not None:
            await offline.review_closed_positions()
        else:
            await outcome_worker.tick()

    controller.add_job(PeriodicJob("closed_position_review", 60, review_closed_positions))

    async def operational_snapshot() -> OperationalSnapshot:
        await monitor_account_state()
        if offline is not None:
            return await snapshot_from_view(
                view, audit, trading_clock, realized_pnl=offline.broker.ledger.realized_pnl
            )
        return await snapshot_from_view(view, audit, trading_clock)

    reporter = RuntimeReporter(view, audit, trading_clock, operational_snapshot)
    controller.add_job(PeriodicJob("operational_reporting", 60, reporter.tick))
    application = create_app(
        loaded.app,
        view,
        audit,
        offline.clock if offline is not None else clock,
        metrics,
        dashboard_token=dashboard_token,
        reconcile=controller.reconcile,
        federal_repository=audit,
        reference_clock=clock,
    )
    runtime = ApplicationRuntime(
        loaded,
        database,
        audit,
        view,
        controller,
        broker,
        provider,
        application,
        offline,
        market_provider=market_provider,
    )

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        try:
            await controller.start()
            yield
        finally:
            await runtime.close()

    application.router.lifespan_context = lifespan
    return runtime
