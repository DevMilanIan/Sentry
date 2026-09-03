from __future__ import annotations

from app.domain.enums import OrderState


class InvalidOrderTransition(ValueError):
    pass


class OrderStateMachine:
    """Single source of truth for durable order lifecycle transitions."""

    _transitions: dict[OrderState, frozenset[OrderState]] = {
        OrderState.PROPOSED: frozenset({OrderState.REVIEWED, OrderState.REJECTED}),
        OrderState.REVIEWED: frozenset({OrderState.APPROVED, OrderState.REJECTED}),
        OrderState.APPROVED: frozenset({OrderState.INTENT_PERSISTED, OrderState.REJECTED}),
        OrderState.INTENT_PERSISTED: frozenset({OrderState.SUBMITTING, OrderState.REJECTED}),
        OrderState.SUBMITTING: frozenset(
            {
                OrderState.SUBMISSION_UNKNOWN,
                OrderState.OPEN,
                OrderState.PARTIAL,
                OrderState.FILLED,
                OrderState.CANCELED,
                OrderState.REJECTED,
            }
        ),
        OrderState.SUBMISSION_UNKNOWN: frozenset(
            {
                OrderState.OPEN,
                OrderState.PARTIAL,
                OrderState.FILLED,
                OrderState.CANCELED,
                OrderState.REJECTED,
                OrderState.EXPIRED,
            }
        ),
        OrderState.OPEN: frozenset(
            {OrderState.PARTIAL, OrderState.FILLED, OrderState.CANCELED, OrderState.EXPIRED}
        ),
        OrderState.PARTIAL: frozenset(
            {OrderState.PARTIAL, OrderState.FILLED, OrderState.CANCELED, OrderState.EXPIRED}
        ),
        OrderState.FILLED: frozenset(),
        OrderState.CANCELED: frozenset(),
        OrderState.REJECTED: frozenset(),
        OrderState.EXPIRED: frozenset(),
    }

    terminal_states = frozenset(
        {OrderState.FILLED, OrderState.CANCELED, OrderState.REJECTED, OrderState.EXPIRED}
    )

    @classmethod
    def can_transition(
        cls,
        current: OrderState,
        target: OrderState,
        *,
        reconciliation_evidence: bool = False,
    ) -> bool:
        if current is OrderState.SUBMISSION_UNKNOWN and not reconciliation_evidence:
            return False
        return target in cls._transitions[current]

    @classmethod
    def transition(
        cls,
        current: OrderState,
        target: OrderState,
        *,
        reconciliation_evidence: bool = False,
    ) -> OrderState:
        if not cls.can_transition(current, target, reconciliation_evidence=reconciliation_evidence):
            suffix = (
                " without reconciliation evidence"
                if current is OrderState.SUBMISSION_UNKNOWN
                else ""
            )
            raise InvalidOrderTransition(
                f"invalid order transition {current.value}->{target.value}{suffix}"
            )
        return target

    @classmethod
    def is_terminal(cls, state: OrderState) -> bool:
        return state in cls.terminal_states
