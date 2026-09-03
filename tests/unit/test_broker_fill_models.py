from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal
from uuid import uuid4

from app.broker.fill_models import ConservativeFillModel, OptimisticFillModel
from app.domain.enums import ExecutionEnvironment, OptionType, OrderSide, OrderState
from app.domain.models import BrokerOrder, OptionContract, OptionQuote, ProviderMetadata

NOW = datetime(2026, 9, 2, 15, 0, tzinfo=UTC)
CONTRACT = OptionContract(
    instrument_id="opt-1",
    symbol="TEST",
    option_type=OptionType.CALL,
    strike=Decimal("10"),
    expiration=date(2026, 9, 18),
)


def order(*, quantity: int = 3) -> BrokerOrder:
    return BrokerOrder(
        created_at=NOW,
        intent_id=uuid4(),
        environment=ExecutionEnvironment.DEMO,
        state=OrderState.OPEN,
        contract=CONTRACT,
        side=OrderSide.BUY_TO_OPEN,
        quantity=quantity,
        limit_price=Decimal("0.08"),
        submitted_at=NOW,
    )


def quote(ask: str, *, ask_size: int | None = 2) -> OptionQuote:
    return OptionQuote(
        contract=CONTRACT,
        bid=Decimal("0.06"),
        ask=Decimal(ask),
        last=Decimal(ask),
        bid_size=10,
        ask_size=ask_size,
        metadata=ProviderMetadata(
            provider="fixture",
            capability_version="v1",
            observed_at=NOW,
            effective_at=NOW,
        ),
    )


def test_optimistic_model_fills_full_quantity_on_touch() -> None:
    decision = OptimisticFillModel().evaluate(
        order=order(), quote=quote("0.08"), remaining_quantity=3
    )

    assert decision.quantity == 3
    assert decision.price == Decimal("0.08")
    assert decision.should_fill


def test_conservative_model_requires_more_than_touch_when_queue_gate_is_zero() -> None:
    model = ConservativeFillModel(touch_fill_probability=Decimal("0"))

    touched = model.evaluate(order=order(), quote=quote("0.08"), remaining_quantity=3)
    improved = model.evaluate(order=order(), quote=quote("0.07"), remaining_quantity=3)

    assert not touched.should_fill
    assert improved.quantity == 1
    assert improved.price == Decimal("0.07")
    assert model.evaluate(order=order(), quote=quote("0.09"), remaining_quantity=3).quantity == 0


def test_fill_seed_is_reproducible_from_order_quote_and_model_version() -> None:
    model = ConservativeFillModel()
    broker_order = order()
    market = quote("0.07")

    first = model.evaluate(order=broker_order, quote=market, remaining_quantity=3)
    second = model.evaluate(order=broker_order, quote=market, remaining_quantity=3)

    assert first == second


def test_configured_seed_salt_changes_the_replay_seed() -> None:
    broker_order = order()
    market = quote("0.07")
    first = ConservativeFillModel(seed_salt=1729).evaluate(
        order=broker_order, quote=market, remaining_quantity=3
    )
    second = ConservativeFillModel(seed_salt=1730).evaluate(
        order=broker_order, quote=market, remaining_quantity=3
    )

    assert first.deterministic_seed != second.deterministic_seed
