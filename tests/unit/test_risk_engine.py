from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

from app.clock.base import VirtualClock
from app.config import load_config
from app.domain.enums import AccountKind, ExecutionEnvironment, OptionType, OrderSide
from app.domain.models import (
    AccountSnapshot,
    OptionContract,
    OptionQuote,
    ProviderMetadata,
    TradeProposal,
)
from app.risk import RiskEngine, RiskRule

NOW = datetime(2026, 9, 2, 15, 0, tzinfo=UTC)


def quote(*, observed_at: datetime = NOW, bid: str = "0.07", ask: str = "0.08") -> OptionQuote:
    return OptionQuote(
        contract=OptionContract(
            instrument_id="opt-1",
            symbol="TEST",
            option_type=OptionType.CALL,
            strike=Decimal("10"),
            expiration=date(2026, 9, 18),
        ),
        bid=Decimal(bid),
        ask=Decimal(ask),
        last=Decimal(ask),
        volume=100,
        open_interest=500,
        bid_size=10,
        ask_size=10,
        metadata=ProviderMetadata(
            provider="fixture",
            capability_version="v1",
            observed_at=observed_at,
            effective_at=observed_at,
        ),
    )


def proposal(market: OptionQuote, *, price: str = "0.08") -> TradeProposal:
    return TradeProposal(
        created_at=NOW,
        environment=ExecutionEnvironment.DEMO,
        namespace="demo-test",
        packet_id="00000000-0000-0000-0000-000000000001",
        symbol="TEST",
        contract=market.contract,
        side=OrderSide.BUY_TO_OPEN,
        quantity=1,
        limit_price=Decimal(price),
        quote_snapshot_id=market.snapshot_id,
        quote_as_of=market.metadata.observed_at,
        policy_version="demo-exploratory-v1",
        risk_config_version="risk-v1",
        thesis="fixture",
        invalidation_conditions=("fixture invalidated",),
    )


def account(*, as_of: datetime = NOW, cash: str = "25", risk: str = "0") -> AccountSnapshot:
    return AccountSnapshot(
        created_at=as_of,
        environment=ExecutionEnvironment.DEMO,
        account_kind=AccountKind.SHADOW,
        cash=Decimal(cash),
        buying_power=Decimal(cash),
        open_option_risk=Decimal(risk),
        open_positions=0,
        new_entries_today=0,
        as_of=as_of,
        is_authenticated=False,
        state_known=True,
    )


def test_all_hard_rules_pass_for_exact_fresh_small_debit() -> None:
    market = quote()
    engine = RiskEngine(load_config().risk, VirtualClock(NOW))

    decision = engine.evaluate(proposal(market), account(), market)

    assert decision.allowed
    assert decision.failed_rules == ()
    assert decision.proposed_max_loss == Decimal("8.00")
    assert decision.resulting_aggregate_risk == Decimal("8.00")
    assert decision.data_fresh


def test_premium_and_aggregate_limits_cannot_be_bypassed_by_cash() -> None:
    market = quote(bid="0.10", ask="0.11")
    engine = RiskEngine(load_config().risk, VirtualClock(NOW))

    decision = engine.evaluate(proposal(market, price="0.11"), account(cash="1000"), market)

    assert not decision.allowed
    assert RiskRule.PREMIUM_LIMIT.value in decision.failed_rules
    assert RiskRule.AGGREGATE_RISK_LIMIT.value in decision.failed_rules


def test_stale_evidence_fails_closed() -> None:
    stale = NOW - timedelta(minutes=10)
    market = quote(observed_at=stale)
    engine = RiskEngine(load_config().risk, VirtualClock(NOW))

    decision = engine.evaluate(proposal(market), account(as_of=stale), market)

    assert not decision.allowed
    assert RiskRule.ACCOUNT_FRESH.value in decision.failed_rules
    assert RiskRule.QUOTE_FRESH.value in decision.failed_rules
    assert not decision.data_fresh


def test_real_zero_balance_is_not_used_when_shadow_account_is_effective() -> None:
    market = quote()
    engine = RiskEngine(load_config().risk, VirtualClock(NOW))
    real_zero = AccountSnapshot(
        created_at=NOW,
        environment=ExecutionEnvironment.DEMO,
        account_kind=AccountKind.BROKER_OBSERVED,
        cash=Decimal("0"),
        buying_power=Decimal("0"),
        as_of=NOW,
        is_authenticated=True,
        state_known=True,
    )

    shadow_decision = engine.evaluate(proposal(market), account(), market)
    real_decision = engine.evaluate(proposal(market), real_zero, market)

    assert shadow_decision.allowed
    assert not real_decision.allowed
    assert RiskRule.CASH_AFFORDABLE.value in real_decision.failed_rules
