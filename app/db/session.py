from __future__ import annotations

import re
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from typing import cast
from uuid import uuid4

from sqlalchemy import Table, insert, text
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app.config import RuntimeBinding
from app.db.models import ENVIRONMENT_MODELS, SHARED_MODELS, Base
from app.exceptions import ConfigurationError

_SAFE_IDENTIFIER = re.compile(r"^[a-z][a-z0-9_]{0,62}$")


def _validate_schema(value: str) -> str:
    if not _SAFE_IDENTIFIER.fullmatch(value):
        raise ConfigurationError(f"unsafe PostgreSQL schema identifier: {value!r}")
    return value


class DatabaseManager:
    """Environment-bound async PostgreSQL manager with schema translation."""

    def __init__(self, url: str, binding: RuntimeBinding, shared_schema: str = "shared") -> None:
        self.binding = binding
        self.shared_schema = _validate_schema(shared_schema)
        self.environment_schema = _validate_schema(binding.database_schema)
        self.engine: AsyncEngine = create_async_engine(
            url,
            pool_pre_ping=True,
            future=True,
            execution_options={
                "schema_translate_map": {
                    "shared": self.shared_schema,
                    "environment": self.environment_schema,
                }
            },
        )
        self._factory = async_sessionmaker(self.engine, expire_on_commit=False, class_=AsyncSession)

    async def healthcheck(self) -> bool:
        try:
            async with self.engine.connect() as connection:
                transaction = await connection.begin()
                try:
                    table = cast(Table, ENVIRONMENT_MODELS["health_events"].__table__)
                    await connection.execute(
                        insert(table).values(
                            id=uuid4(),
                            created_at=datetime.now(UTC),
                            run_id=None,
                            record_type="DatabaseWriteHealthcheck",
                            environment=self.binding.environment.value,
                            namespace=self.binding.idempotency_namespace,
                            payload={"probe": True},
                        )
                    )
                finally:
                    await transaction.rollback()
            return True
        except Exception:
            return False

    async def initialize_for_development(self) -> None:
        """Create schemas/tables for local bootstrapping; production upgrades use Alembic."""
        async with self.engine.begin() as connection:
            await connection.execute(text(f'CREATE SCHEMA IF NOT EXISTS "{self.shared_schema}"'))
            await connection.execute(
                text(f'CREATE SCHEMA IF NOT EXISTS "{self.environment_schema}"')
            )
            await connection.run_sync(
                lambda sync_connection: Base.metadata.create_all(
                    sync_connection,
                    tables=[cast(Table, model.__table__) for model in SHARED_MODELS.values()],
                    checkfirst=True,
                )
            )
            await connection.run_sync(
                lambda sync_connection: Base.metadata.create_all(
                    sync_connection,
                    tables=[cast(Table, model.__table__) for model in ENVIRONMENT_MODELS.values()],
                    checkfirst=True,
                )
            )

    @asynccontextmanager
    async def session(self) -> AsyncIterator[AsyncSession]:
        async with self._factory() as session:
            try:
                yield session
                await session.commit()
            except Exception:
                await session.rollback()
                raise

    async def close(self) -> None:
        await self.engine.dispose()
