"""Opt-in Alembic smoke test in an exclusively created disposable database.

Never migrates the database named by SENTRY_TEST_DATABASE_URL. That URL is used
only to create and later remove one uniquely named, ownership-tagged database.
This additional test requires explicit SENTRY_TEST_ALLOW_DATABASE_CREATION=1.
"""

from __future__ import annotations

import asyncio
import os
import re
from collections.abc import AsyncIterator
from uuid import uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.engine import URL, make_url
from sqlalchemy.ext.asyncio import create_async_engine

from app.db.models import ENVIRONMENT_TABLE_NAMES, EXECUTION_UNIQUE_KEYS, SHARED_TABLE_NAMES
from app.main import _upgrade_database

pytestmark = pytest.mark.integration
_DATABASE_NAME = re.compile(r"^sentry_migration_test_[0-9a-f]{32}$")


@pytest.fixture
async def disposable_migration_database() -> AsyncIterator[URL]:
    raw_url = os.environ.get("SENTRY_TEST_DATABASE_URL")
    if not raw_url or os.environ.get("SENTRY_TEST_ALLOW_DATABASE_CREATION") != "1":
        pytest.skip("real migration test requires a PostgreSQL URL and database-creation opt-in")
    try:
        source_url = make_url(raw_url)
    except Exception:
        pytest.fail("SENTRY_TEST_DATABASE_URL is not a valid database URL", pytrace=False)
    if source_url.drivername not in {"postgresql", "postgresql+asyncpg"}:
        pytest.fail("SENTRY_TEST_DATABASE_URL must use PostgreSQL/asyncpg", pytrace=False)
    source_url = source_url.set(drivername="postgresql+asyncpg")
    token = uuid4().hex
    database_name = f"sentry_migration_test_{token}"
    owner_marker = f"Sentry isolated migration test {token}"
    assert _DATABASE_NAME.fullmatch(database_name)
    assert database_name != source_url.database
    admin = create_async_engine(
        source_url,
        isolation_level="AUTOCOMMIT",
        connect_args={"timeout": 10, "command_timeout": 30},
    )
    created = False
    tagged = False
    try:
        async with admin.connect() as connection:
            # No IF NOT EXISTS: a pre-existing database is never adopted.
            await connection.execute(text(f'CREATE DATABASE "{database_name}"'))
            created = True
            await connection.execute(
                text(f'COMMENT ON DATABASE "{database_name}" IS \'{owner_marker}\'')
            )
            tagged = True
        yield source_url.set(database=database_name)
    finally:
        try:
            if created:
                if not tagged:
                    raise RuntimeError("created test database left intact because tagging failed")
                async with admin.connect() as connection:
                    marker = await connection.scalar(
                        text(
                            "SELECT shobj_description(oid, 'pg_database') "
                            "FROM pg_database WHERE datname = :name"
                        ),
                        {"name": database_name},
                    )
                    if marker != owner_marker or not _DATABASE_NAME.fullmatch(database_name):
                        raise RuntimeError("refusing cleanup without exact test database ownership")
                    # Never force-disconnect sessions or drop another database.
                    await connection.execute(text(f'DROP DATABASE "{database_name}"'))
        finally:
            await admin.dispose()


async def test_real_postgres_alembic_fresh_upgrade_and_repeat_are_complete(
    disposable_migration_database: URL,
) -> None:
    database_url = disposable_migration_database.render_as_string(hide_password=False)
    await asyncio.to_thread(_upgrade_database, database_url)
    await asyncio.to_thread(_upgrade_database, database_url)
    engine = create_async_engine(disposable_migration_database)
    try:
        async with engine.connect() as connection:
            assert await connection.scalar(text("SELECT version_num FROM alembic_version")) == (
                "0002_durable_execution"
            )
            for schema, expected_tables in (
                ("shared", SHARED_TABLE_NAMES),
                ("demo", ENVIRONMENT_TABLE_NAMES),
                ("live", ENVIRONMENT_TABLE_NAMES),
            ):
                result = await connection.scalars(
                    text(
                        "SELECT table_name FROM information_schema.tables "
                        "WHERE table_schema = :schema AND table_type = 'BASE TABLE'"
                    ),
                    {"schema": schema},
                )
                assert set(result) == set(expected_tables)
                identities = await connection.execute(
                    text(
                        "SELECT table_name, is_identity, is_nullable "
                        "FROM information_schema.columns "
                        "WHERE table_schema = :schema AND column_name = 'append_sequence'"
                    ),
                    {"schema": schema},
                )
                assert {tuple(row) for row in identities} == {
                    (table, "YES", "NO") for table in expected_tables
                }
                if schema == "shared":
                    continue
                indexes = await connection.scalars(
                    text("SELECT indexname FROM pg_indexes WHERE schemaname = :schema"),
                    {"schema": schema},
                )
                expected_indexes = {
                    f"uq_{table}_{key}"
                    for table, keys in EXECUTION_UNIQUE_KEYS.items()
                    for key in keys
                }
                assert expected_indexes <= set(indexes)
    finally:
        await engine.dispose()
