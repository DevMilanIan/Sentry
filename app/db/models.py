from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import BigInteger, DateTime, Identity, Index, String, Uuid, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column
from sqlalchemy.types import JSON


class Base(DeclarativeBase):
    pass


class _AuditMixin:
    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    append_sequence: Mapped[int] = mapped_column(BigInteger, Identity(), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    run_id: Mapped[UUID | None] = mapped_column(Uuid, nullable=True, index=True)
    record_type: Mapped[str] = mapped_column(String(80), nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(
        JSON().with_variant(JSONB(), "postgresql"), nullable=False
    )


class _SharedAuditRow(_AuditMixin, Base):
    __abstract__ = True


class _EnvironmentAuditRow(_AuditMixin, Base):
    __abstract__ = True
    environment: Mapped[str] = mapped_column(String(8), nullable=False, index=True)
    namespace: Mapped[str] = mapped_column(String(128), nullable=False, index=True)


EXECUTION_UNIQUE_KEYS: dict[str, tuple[str, ...]] = {
    "trade_proposals": ("proposal_id",),
    "risk_decisions": ("decision_id",),
    "approvals": ("approval_id",),
    "broker_reviews": ("review_id",),
    "order_intents": ("intent_id", "order_fingerprint", "idempotency_key"),
    "broker_command_intents": (
        "command_intent_id",
        "order_intent_id",
        "order_fingerprint",
        "idempotency_key",
    ),
    "fills": ("fill_id",),
    "environment_audit_events": ("transition_id",),
    "reconciliation_events": ("reconciliation_id", "negative_reconciliation_id"),
}


def _make_row(class_name: str, table_name: str, base: type[Base], schema: str) -> type[Base]:
    unique_indexes = tuple(
        Index(
            f"uq_{table_name}_{key}",
            "environment",
            "namespace",
            text(f"(payload ->> '{key}')"),
            unique=True,
            postgresql_where=text(f"payload ->> '{key}' IS NOT NULL"),
        )
        for key in EXECUTION_UNIQUE_KEYS.get(table_name, ())
    )
    return type(
        class_name,
        (base,),
        {
            "__tablename__": table_name,
            "__table_args__": (
                Index(f"ix_{table_name}_created_at", "created_at"),
                Index(f"ix_{table_name}_append_sequence", "append_sequence"),
                *unique_indexes,
                {"schema": schema},
            ),
            "__module__": __name__,
        },
    )


SHARED_TABLE_NAMES = (
    "securities",
    "market_snapshots",
    "option_contracts",
    "option_snapshots",
    "source_documents",
    "catalysts",
    "federal_relationships",
    "strategy_versions",
    "configuration_versions",
    "fill_model_versions",
)

ENVIRONMENT_TABLE_NAMES = (
    "system_runs",
    "market_sessions",
    "sentinel_events",
    "candidate_runs",
    "candidate_features",
    "candidate_packets",
    "model_calls",
    "agent_analyses",
    "contract_candidates",
    "risk_decisions",
    "trade_proposals",
    "approvals",
    "broker_reviews",
    "order_intents",
    "broker_command_intents",
    "orders",
    "fills",
    "positions",
    "position_snapshots",
    "trade_outcomes",
    "rejected_candidate_outcomes",
    "health_events",
    "notification_events",
    "environment_audit_events",
    "broker_capability_snapshots",
    "broker_observed_account_snapshots",
    "shadow_account_snapshots",
    "shadow_ledger_events",
    "external_write_firewall_events",
    "decision_policy_versions",
    "demo_live_policy_divergences",
    "replay_runs",
    "reconciliation_events",
    "qualification_runs",
    "qualification_session_summaries",
)


SHARED_MODELS: dict[str, type[Base]] = {}
ENVIRONMENT_MODELS: dict[str, type[Base]] = {}

for _table_name in SHARED_TABLE_NAMES:
    _class_name = "".join(part.title() for part in _table_name.split("_")) + "Row"
    _model = _make_row(_class_name, _table_name, _SharedAuditRow, "shared")
    globals()[_class_name] = _model
    SHARED_MODELS[_table_name] = _model

for _table_name in ENVIRONMENT_TABLE_NAMES:
    _class_name = "".join(part.title() for part in _table_name.split("_")) + "Row"
    _model = _make_row(_class_name, _table_name, _EnvironmentAuditRow, "environment")
    globals()[_class_name] = _model
    ENVIRONMENT_MODELS[_table_name] = _model
