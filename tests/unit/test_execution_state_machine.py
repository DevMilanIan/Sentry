import pytest

from app.domain.enums import OrderState
from app.execution import InvalidOrderTransition, OrderStateMachine


def test_submission_unknown_requires_reconciliation_evidence() -> None:
    with pytest.raises(InvalidOrderTransition, match="reconciliation"):
        OrderStateMachine.transition(OrderState.SUBMISSION_UNKNOWN, OrderState.OPEN)

    assert (
        OrderStateMachine.transition(
            OrderState.SUBMISSION_UNKNOWN,
            OrderState.OPEN,
            reconciliation_evidence=True,
        )
        is OrderState.OPEN
    )


def test_terminal_order_cannot_reopen() -> None:
    with pytest.raises(InvalidOrderTransition):
        OrderStateMachine.transition(OrderState.FILLED, OrderState.OPEN)
