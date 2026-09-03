"""Create shared reference and isolated Demo/Live audit schemas."""

from alembic import op
from sqlalchemy import text

from app.db.models import ENVIRONMENT_MODELS, SHARED_MODELS

revision = "0001_initial"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    for schema in ("shared", "demo", "live"):
        bind.execute(text(f'CREATE SCHEMA IF NOT EXISTS "{schema}"'))
    shared_connection = bind.execution_options(schema_translate_map={"shared": "shared"})
    for model in SHARED_MODELS.values():
        model.__table__.create(shared_connection, checkfirst=True)
    for schema in ("demo", "live"):
        environment_connection = bind.execution_options(
            schema_translate_map={"environment": schema}
        )
        for model in ENVIRONMENT_MODELS.values():
            model.__table__.create(environment_connection, checkfirst=True)


def downgrade() -> None:
    bind = op.get_bind()
    for schema in ("live", "demo", "shared"):
        bind.execute(text(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'))
