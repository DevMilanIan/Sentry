from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from uuid import uuid4

import pytest

from app.clock.base import VirtualClock
from app.config import load_config
from app.db.repository import InMemoryAuditRepository
from app.demo.offline_scenario import _scripted_outputs
from app.domain.enums import ExecutionEnvironment, OrderSide, TradingMode
from app.domain.models import TradeProposal
from app.execution import ExecutionDenied
from app.reasoning.scripted import ScriptedReplayModelProvider
from app.runtime import build_application


@pytest.mark.asyncio
async def test_replay_eof_blocks_dispatch_without_waiting_for_health_tick(
    tmp_path: Path,
) -> None:
    loaded = load_config()
    loaded = loaded.model_copy(
        update={
            "app": loaded.app.model_copy(
                update={
                    "trading_mode": TradingMode.AUTO,
                    "runtime": loaded.app.runtime.model_copy(
                        update={
                            "environment_execution_disabled": False,
                            "startup_health_window_seconds": 0,
                            "disabled_file": tmp_path / "TRADING_DISABLED",
                            "instance_lock_dir": tmp_path / "locks",
                        }
                    ),
                }
            )
        }
    )
    repository = InMemoryAuditRepository(loaded.bind_runtime())
    runtime = await build_application(
        loaded,
        repository=repository,
        model_provider=ScriptedReplayModelProvider(_scripted_outputs()),
        wall_clock=VirtualClock(datetime(2026, 9, 3, tzinfo=UTC)),
    )
    try:
        offline = runtime.offline
        assert offline is not None
        assert await runtime.controller.reconcile()
        await offline.step()
        await offline.step()
        await runtime.controller.health_once()
        await runtime.controller.health_once()
        assert runtime.view.safety.permits_new_entry()

        while not offline.session.complete:
            await offline.step()

        assert not runtime.view.market_data_fresh
        assert not runtime.view.safety.permits_new_entry()
        quote = await offline.session.market.get_option_quote("ACME-20260123-C-10.25")
        proposal = TradeProposal(
            created_at=offline.clock.now(),
            environment=ExecutionEnvironment.DEMO,
            namespace=runtime.view.binding.idempotency_namespace,
            packet_id=uuid4(),
            symbol=quote.contract.symbol,
            contract=quote.contract,
            side=OrderSide.BUY_TO_OPEN,
            quantity=1,
            limit_price=Decimal("0.08"),
            quote_snapshot_id=quote.snapshot_id,
            quote_as_of=quote.metadata.observed_at,
            policy_version="test-eof-guard",
            risk_config_version=loaded.risk.version,
            thesis="must not be transmitted after finite replay exhaustion",
            invalidation_conditions=("fixture complete",),
        )
        await offline.add_proposal(proposal)
        await offline.dispatch_proposals()
        assert await offline.broker.get_orders() == ()
        with pytest.raises(ExecutionDenied):
            await offline.execution.execute(proposal)
    finally:
        await runtime.close()
