"""Whole-path fixture evidence for broker-shadow composition, never platform validation."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any
from uuid import uuid4

from app.api.dashboard import RuntimeView
from app.broker.base import BrokerCapabilities, validate_command_for_capability
from app.broker.shadow_runtime import BrokerShadowRuntime
from app.broker.simulated import SimulatedBroker
from app.clock.base import VirtualClock
from app.config import LoadedConfig, load_config
from app.db.repository import InMemoryAuditRepository
from app.demo.offline_scenario import _scripted_outputs
from app.domain.enums import (
    AccountKind,
    DemoBackend,
    ExecutionEnvironment,
    OptionType,
    OrderSide,
    OrderState,
    RuntimeSafetyState,
    TradingMode,
)
from app.domain.models import (
    AccountSnapshot,
    BrokerCommandIntent,
    BrokerOrder,
    BrokerReview,
    EquityQuote,
    OptionContract,
    OptionQuote,
    Position,
    ProviderMetadata,
    TradeProposal,
)
from app.market.base import MarketDataProvider
from app.market.models import EquityScanRequest, MarketDataCapabilities, PriceBar
from app.reasoning.scripted import ScriptedReplayModelProvider
from app.safety.runtime_state import SafetyController, SafetyEvidence

NOW = datetime(2026, 9, 1, 14, 0, tzinfo=UTC)


class FixtureCurrentMarket(MarketDataProvider):
    """Deterministic fixture implementing the current-provider contract only."""

    def __init__(self, clock: VirtualClock) -> None:
        self.clock = clock
        self.contract = OptionContract(
            instrument_id="fixture-option-1",
            symbol="TEST",
            option_type=OptionType.CALL,
            strike=Decimal("10"),
            expiration=date(2026, 9, 18),
        )
        self.option = self._option("0.06", "0.08")

    @property
    def identity(self) -> str:
        return "fixture-current-provider-not-robinhood"

    @property
    def capability_version(self) -> str:
        return "fixture-schema-v1"

    @property
    def capabilities(self) -> MarketDataCapabilities:
        return MarketDataCapabilities(replay=False)

    def _metadata(self) -> ProviderMetadata:
        return ProviderMetadata(
            provider=self.identity,
            capability_version=self.capability_version,
            observed_at=self.clock.now(),
            effective_at=self.clock.now(),
        )

    def _option(self, bid: str, ask: str) -> OptionQuote:
        return OptionQuote(
            contract=self.contract,
            bid=Decimal(bid),
            ask=Decimal(ask),
            last=Decimal(ask),
            volume=100,
            open_interest=500,
            bid_size=4,
            ask_size=4,
            metadata=self._metadata(),
        )

    def move_option(self, bid: str, ask: str) -> None:
        self.option = self._option(bid, ask)

    async def get_equity_quote(
        self, symbol: str, *, as_of: datetime | None = None
    ) -> EquityQuote:
        del as_of
        assert symbol == "TEST"
        return EquityQuote(
            symbol=symbol,
            bid=Decimal("9.99"),
            ask=Decimal("10.01"),
            last=Decimal("10"),
            volume=10_000,
            metadata=self._metadata(),
        )

    async def get_bars(
        self,
        symbol: str,
        *,
        start: datetime | None = None,
        end: datetime | None = None,
        timeframe: str | None = None,
        as_of: datetime | None = None,
    ) -> Sequence[PriceBar]:
        del symbol, start, end, timeframe, as_of
        return ()

    async def get_option_chain(
        self, symbol: str, *, as_of: datetime | None = None
    ) -> Sequence[OptionQuote]:
        del as_of
        assert symbol == "TEST"
        return (self.option,)

    async def get_option_quote(
        self, instrument_id: str, *, as_of: datetime | None = None
    ) -> OptionQuote:
        del as_of
        assert instrument_id == self.contract.instrument_id
        return self.option

    async def scan_equities(
        self, request: EquityScanRequest, *, as_of: datetime | None = None
    ) -> Sequence[EquityQuote]:
        del request, as_of
        return ()


class FixtureRobinhoodReadReview:
    """A read/review fixture with deliberately no generic or write transport."""

    def __init__(self, clock: VirtualClock) -> None:
        self.clock = clock
        self._schemas = SimulatedBroker(clock=clock)
        self.account = AccountSnapshot(
            created_at=clock.now(),
            environment=ExecutionEnvironment.DEMO,
            account_kind=AccountKind.BROKER_OBSERVED,
            account_fingerprint="fixture-agentic-account",
            cash=Decimal("0"),
            buying_power=Decimal("0"),
            as_of=clock.now(),
            is_authenticated=True,
            state_known=True,
        )
        self.positions: tuple[Position, ...] = ()
        self.orders: tuple[BrokerOrder, ...] = ()
        self.mutate_during_review = False

    async def get_capabilities(self) -> BrokerCapabilities:
        return await self._schemas.get_capabilities()

    async def get_account_state(self) -> AccountSnapshot:
        return self.account.model_copy(
            update={"created_at": self.clock.now(), "as_of": self.clock.now()}
        )

    async def get_positions(self) -> tuple[Position, ...]:
        return self.positions

    async def get_orders(self) -> tuple[BrokerOrder, ...]:
        return self.orders

    async def review_option_order(self, proposal: TradeProposal) -> BrokerReview:
        if self.mutate_during_review:
            self.orders = (
                BrokerOrder(
                    created_at=self.clock.now(),
                    broker_order_id="unexpected-real-order",
                    intent_id=uuid4(),
                    environment=ExecutionEnvironment.DEMO,
                    state=OrderState.OPEN,
                    contract=proposal.contract,
                    side=proposal.side,
                    quantity=proposal.quantity,
                    limit_price=proposal.limit_price,
                    submitted_at=self.clock.now(),
                ),
            )
        return BrokerReview(
            created_at=self.clock.now(),
            environment=ExecutionEnvironment.DEMO,
            proposal_id=proposal.proposal_id,
            accepted=False,
            warnings=("fixture real account has zero buying power",),
            side_effect_free=True,
        )

    async def validate_command(self, command: BrokerCommandIntent) -> dict[str, Any]:
        return validate_command_for_capability(command, await self.get_capabilities())


def shadow_config(tmp_path: Path) -> LoadedConfig:
    loaded = load_config()
    runtime = loaded.app.runtime.model_copy(
        update={
            "environment_execution_disabled": False,
            "disabled_file": tmp_path / "kill-switch-not-present",
            "startup_health_window_seconds": 0,
        }
    )
    return loaded.model_copy(
        update={
            "app": loaded.app.model_copy(
                update={
                    "execution_environment": ExecutionEnvironment.DEMO,
                    "demo_backend": DemoBackend.BROKER_SHADOW,
                    "trading_mode": TradingMode.SHADOW,
                    "runtime": runtime,
                }
            )
        }
    )


async def runtime_fixture(
    tmp_path: Path,
    clock: VirtualClock,
    market: FixtureCurrentMarket,
    client: FixtureRobinhoodReadReview,
    repository: InMemoryAuditRepository | None = None,
) -> tuple[BrokerShadowRuntime, InMemoryAuditRepository]:
    loaded = shadow_config(tmp_path)
    repository = repository or InMemoryAuditRepository(loaded.bind_runtime())
    safety = SafetyController(clock, timedelta(0))
    view = RuntimeView(
        binding=loaded.bind_runtime(),
        trading_mode=TradingMode.SHADOW,
        safety=safety,
        write_firewall="DENY_ALL_WRITES",
    )
    runtime = await BrokerShadowRuntime.create(
        loaded,
        repository,
        view,
        clock,
        read_client=client,
        market=market,
        model_provider=ScriptedReplayModelProvider(_scripted_outputs()),
        market_watchlist=("TEST",),
        expected_account_fingerprint="fixture-agentic-account",
    )
    return runtime, repository


def proposal(market: FixtureCurrentMarket, clock: VirtualClock, namespace: str) -> TradeProposal:
    quote = market.option
    return TradeProposal(
        created_at=clock.now(),
        environment=ExecutionEnvironment.DEMO,
        namespace=namespace,
        packet_id=uuid4(),
        symbol="TEST",
        contract=market.contract,
        side=OrderSide.BUY_TO_OPEN,
        quantity=1,
        limit_price=Decimal("0.08"),
        quote_snapshot_id=quote.snapshot_id,
        quote_as_of=quote.metadata.observed_at,
        policy_version="fixture-demo-exploratory-v1",
        risk_config_version="risk-v1",
        thesis="fixture evidence only",
        invalidation_conditions=("fixture invalidated",),
    )


def permit_shadow_entry(runtime: BrokerShadowRuntime) -> None:
    evidence = SafetyEvidence(
        database_writable=True,
        broker_state_known=True,
        reconciled=True,
        market_data_fresh=True,
        account_data_fresh=True,
        execution_service_healthy=True,
        kill_switch_clear=True,
        environment_matches=True,
    )
    runtime.view.safety.observe(evidence)
    runtime.view.safety.observe(evidence)
    assert runtime.view.safety.state is RuntimeSafetyState.NORMAL


async def test_exact_shadow_intent_denial_fill_and_restart_are_one_persisted_path(
    tmp_path: Path,
) -> None:
    """Fixtures prove local orchestration, not Robinhood connectivity or schema validity."""

    clock = VirtualClock(NOW)
    market = FixtureCurrentMarket(clock)
    client = FixtureRobinhoodReadReview(clock)
    runtime, repository = await runtime_fixture(tmp_path, clock, market, client)

    assert await runtime.reconcile()
    await runtime.scan_current_market()
    assert await runtime.health()
    permit_shadow_entry(runtime)

    trade = proposal(market, clock, runtime.binding.idempotency_namespace)
    await runtime.add_proposal(trade)
    await runtime.dispatch_proposals()

    orders = await runtime.broker.get_orders()
    assert len(orders) == 1 and orders[0].state is OrderState.OPEN
    command_rows = await repository.list("broker_command_intents")
    firewall_rows = await repository.list("external_write_firewall_events")
    review_rows = [
        row
        for row in await repository.list("broker_reviews")
        if row["payload"].get("record_kind") == "broker_shadow_review_evidence_v1"
    ]
    assert len(command_rows) == len(firewall_rows) == len(review_rows) == 1
    command = BrokerCommandIntent.model_validate(command_rows[0]["payload"])
    order_intent = (await repository.list("order_intents"))[0]["payload"]
    assert firewall_rows[0]["payload"]["transmitted"] is False
    assert firewall_rows[0]["payload"]["command_hash"] == command.command_hash
    evidence = review_rows[0]["payload"]
    assert evidence["shadow_review"]["accepted"] is True
    assert evidence["broker_observed_review"]["accepted"] is False
    assert evidence["observed_state_unchanged"] is True
    assert evidence["combined_review_id"] == order_intent["review_id"]
    assert str(command.proposal_id) == evidence["proposal_id"]
    assert not hasattr(runtime.broker, "_write_transport")
    assert not hasattr(client, "call_tool")

    restored, _ = await runtime_fixture(tmp_path, clock, market, client, repository)
    assert await restored.reconcile()
    assert await restored.broker.get_orders() == orders
    assert restored.expected_account_fingerprint == "fixture-agentic-account"

    await clock.advance(timedelta(seconds=1))
    market.move_option("0.05", "0.07")
    await restored.monitor_shadow_positions()
    filled = await restored.broker.get_orders()
    assert filled[0].state is OrderState.FILLED
    assert len(await restored.store.list_fills(filled[0].order_id)) == 1
    assert len(await restored.store.list_positions()) == 1
    assert not await restored.store.unresolved_intents()
    assert (await restored.broker.get_observed_broker_account_state()).cash == Decimal("0")
    assert (await restored.broker.get_effective_execution_account_state()).cash == Decimal("18")


async def test_observed_mutation_during_safe_review_halts_before_command_or_denial(
    tmp_path: Path,
) -> None:
    clock = VirtualClock(NOW)
    market = FixtureCurrentMarket(clock)
    client = FixtureRobinhoodReadReview(clock)
    runtime, repository = await runtime_fixture(tmp_path, clock, market, client)
    assert await runtime.reconcile()
    await runtime.scan_current_market()
    permit_shadow_entry(runtime)
    client.mutate_during_review = True

    await runtime.add_proposal(proposal(market, clock, runtime.binding.idempotency_namespace))
    await runtime.dispatch_proposals()

    assert runtime.view.safety.state is RuntimeSafetyState.HALTED
    assert await repository.list("broker_command_intents") == []
    assert await repository.list("external_write_firewall_events") == []
    review = next(
        row["payload"]
        for row in await repository.list("broker_reviews")
        if row["payload"].get("record_kind") == "broker_shadow_review_evidence_v1"
    )
    assert review["record_kind"] == "broker_shadow_review_evidence_v1"
    assert review["observed_state_unchanged"] is False
    assert await runtime.broker.get_orders() == ()
