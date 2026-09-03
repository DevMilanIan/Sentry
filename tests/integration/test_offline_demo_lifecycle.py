from __future__ import annotations

from decimal import Decimal

import pytest

from app.demo.offline_scenario import run_offline_demo_scenario
from app.domain.enums import ExecutionEnvironment, OrderState


@pytest.mark.asyncio
async def test_complete_offline_demo_lifecycle_is_causal_audited_and_reproducible() -> None:
    first = await run_offline_demo_scenario()
    second = await run_offline_demo_scenario()

    assert first.environment is ExecutionEnvironment.DEMO
    assert first.reasoning_roles == ("situation", "bull", "bear", "skeptic", "judge")
    assert first.reasoning_status == "JUDGED"
    assert first.policy_proceeded
    assert first.selected_instrument_id == "ACME-20260123-C-10.25"
    assert first.rejected_instrument_ids == ("ACME-20260123-C-12",)
    assert first.entry_submission_state is OrderState.OPEN
    assert first.entry_final_state is OrderState.FILLED
    assert first.exit_submission_state is OrderState.OPEN
    assert first.exit_final_state is OrderState.FILLED
    assert first.entry_fill_price == Decimal("0.08")
    assert first.exit_fill_price == Decimal("0.14")
    assert first.realized_pnl == Decimal("6.00")
    assert first.final_cash == Decimal("31.00")
    assert first.final_positions == 0
    assert first.final_open_orders == 0
    assert first.no_lookahead_proven
    assert first.same_event_fill_blocked

    assert first.audit_counts["model_calls"] == 5
    assert first.audit_counts["risk_decisions"] == 2
    assert first.audit_counts["order_intents"] == 2
    assert first.audit_counts["broker_command_intents"] == 2
    assert first.audit_counts["fills"] == 2
    assert first.audit_counts["positions"] == 1
    assert first.audit_counts["trade_outcomes"] == 1
    assert first.audit_counts["reconciliation_events"] == 1

    assert first.fill_seeds == second.fill_seeds
    assert first.semantic_journal_hash == second.semantic_journal_hash
