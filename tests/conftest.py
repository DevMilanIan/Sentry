from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path
from uuid import uuid4

import pytest

from app.clock.base import VirtualClock
from app.config import RuntimeBinding
from app.domain.enums import DemoBackend, ExecutionEnvironment, OptionType, OrderSide
from app.domain.models import OptionContract, TradeProposal


@pytest.fixture
def instant() -> datetime:
    return datetime(2026, 9, 1, 14, 0, tzinfo=UTC)


@pytest.fixture
def clock(instant: datetime) -> VirtualClock:
    return VirtualClock(instant)


@pytest.fixture
def demo_binding(tmp_path: Path) -> RuntimeBinding:
    return RuntimeBinding(
        environment=ExecutionEnvironment.DEMO,
        demo_backend=DemoBackend.OFFLINE_SIM,
        database_schema="demo",
        runtime_directory=tmp_path / "demo",
        idempotency_namespace="demo-test",
        external_write_authority=False,
        config_version="test-v1",
    )


@pytest.fixture
def proposal(instant: datetime) -> TradeProposal:
    contract = OptionContract(
        instrument_id="opt-test-1",
        symbol="TEST",
        option_type=OptionType.CALL,
        strike=Decimal("10"),
        expiration=date(2026, 9, 18),
    )
    return TradeProposal(
        created_at=instant,
        environment=ExecutionEnvironment.DEMO,
        namespace="demo-test",
        packet_id=uuid4(),
        symbol="TEST",
        contract=contract,
        side=OrderSide.BUY_TO_OPEN,
        quantity=1,
        limit_price=Decimal("0.08"),
        quote_snapshot_id=uuid4(),
        quote_as_of=instant,
        policy_version="demo-v1",
        risk_config_version="risk-v1",
        thesis="fixture thesis",
        invalidation_conditions=("fixture invalidated",),
    )
