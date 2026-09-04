"""Uniquely identify immutable closed-position reviews in each namespace."""

from alembic import op

revision = "0003_closed_trade_outcomes"
down_revision = "0002_durable_execution"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Legacy scenario-summary rows have no outcome_id and remain untouched.
    for schema in ("demo", "live"):
        op.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS uq_trade_outcomes_outcome_id "
            f"ON {schema}.trade_outcomes "
            "(environment, namespace, (payload ->> 'outcome_id')) "
            "WHERE payload ->> 'outcome_id' IS NOT NULL"
        )


def downgrade() -> None:
    for schema in ("demo", "live"):
        op.execute(f"DROP INDEX IF EXISTS {schema}.uq_trade_outcomes_outcome_id")
