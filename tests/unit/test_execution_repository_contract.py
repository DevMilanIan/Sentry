from __future__ import annotations

from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from datetime import datetime, timedelta
from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from uuid import uuid4

import pytest
from sqlalchemy import Table
from sqlalchemy.dialects import postgresql
from sqlalchemy.engine.interfaces import Dialect
from sqlalchemy.schema import CreateIndex, CreateTable

from app.config import RuntimeBinding
from app.db.models import (
    ENVIRONMENT_MODELS,
    ENVIRONMENT_TABLE_NAMES,
    EXECUTION_UNIQUE_KEYS,
    SHARED_MODELS,
    SHARED_TABLE_NAMES,
)
from app.db.repository import InMemoryAuditRepository, PostgresAuditRepository
from app.db.session import DatabaseManager
from app.exceptions import SafetyCriticalError


def pg_dialect() -> Dialect:
    return cast(Callable[[], Dialect], postgresql.dialect)()


class ScalarRows:
    def __init__(self, rows: list[Any]) -> None:
        self.rows = rows

    def all(self) -> list[Any]:
        return self.rows


class CapturingSession:
    def __init__(self, rows: list[Any]) -> None:
        self.rows = rows
        self.statements: list[Any] = []

    async def scalars(self, statement: Any) -> ScalarRows:
        self.statements.append(statement)
        return ScalarRows(self.rows)


class CapturingManager:
    def __init__(self, binding: RuntimeBinding, rows: list[Any]) -> None:
        self.binding = binding
        self.capture = CapturingSession(rows)

    @asynccontextmanager
    async def session(self) -> AsyncIterator[CapturingSession]:
        yield self.capture


@pytest.mark.asyncio
async def test_postgres_payload_queries_scope_environment_namespace_and_sequence(
    demo_binding: RuntimeBinding,
    instant: datetime,
) -> None:
    row = SimpleNamespace(
        id=uuid4(),
        append_sequence=17,
        created_at=instant,
        run_id=None,
        record_type="OrderIntent",
        environment="DEMO",
        namespace="demo-test",
        payload={"intent_id": "intent-1"},
    )
    manager = CapturingManager(demo_binding, [row])
    repository = PostgresAuditRepository(cast(DatabaseManager, manager))

    found = await repository.find_payload("order_intents", "intent_id", "intent-1")
    assert found is not None
    assert found["append_sequence"] == 17
    compiled = manager.capture.statements[-1].compile(dialect=pg_dialect())
    sql = str(compiled)
    assert "order_intents.environment =" in sql
    assert "order_intents.namespace =" in sql
    assert "ORDER BY environment.order_intents.append_sequence DESC" in sql
    assert "->>" in sql
    assert "DEMO" in compiled.params.values()
    assert "demo-test" in compiled.params.values()
    assert "intent-1" in compiled.params.values()

    await repository.list_payloads("orders", before_sequence=17, limit=2)
    compiled = manager.capture.statements[-1].compile(dialect=pg_dialect())
    assert "orders.append_sequence <" in str(compiled)
    assert 17 in compiled.params.values()
    assert 2 in compiled.params.values()


@pytest.mark.parametrize("limit", [0, -1, 10001])
@pytest.mark.asyncio
async def test_postgres_query_limits_are_validated_before_database_access(
    demo_binding: RuntimeBinding,
    limit: int,
) -> None:
    manager = CapturingManager(demo_binding, [])
    repository = PostgresAuditRepository(cast(DatabaseManager, manager))
    with pytest.raises(ValueError, match="query limit"):
        await repository.list_payloads("orders", limit=limit)
    assert manager.capture.statements == []


@pytest.mark.asyncio
async def test_memory_repository_paginates_by_append_order_not_business_time(
    demo_binding: RuntimeBinding,
    instant: datetime,
) -> None:
    repository = InMemoryAuditRepository(demo_binding)
    for index in range(3):
        await repository.append(
            "orders",
            {
                "created_at": instant - timedelta(days=index),
                "intent_id": "same",
                "state": str(index),
            },
        )
    newest = await repository.find_payload("orders", "intent_id", "same")
    assert newest is not None
    assert newest["payload"]["state"] == "2"
    first = await repository.list_payloads("orders", limit=2)
    assert [row["append_sequence"] for row in first] == [3, 2]
    remaining = await repository.list_payloads("orders", before_sequence=2)
    assert [row["append_sequence"] for row in remaining] == [1]


@pytest.mark.asyncio
async def test_repository_refuses_cross_namespace_payloads(
    demo_binding: RuntimeBinding,
    instant: datetime,
) -> None:
    repository = InMemoryAuditRepository(demo_binding)
    with pytest.raises(SafetyCriticalError, match="cross-namespace"):
        await repository.append("order_intents", {"created_at": instant, "namespace": "foreign"})


def test_metadata_has_ingestion_identity_and_filtered_execution_unique_indexes() -> None:
    for model in (*ENVIRONMENT_MODELS.values(), *SHARED_MODELS.values()):
        table = cast(Table, model.__table__)
        assert table.c.append_sequence.identity is not None
        ddl = str(CreateTable(table).compile(dialect=pg_dialect()))
        assert "append_sequence BIGINT GENERATED BY DEFAULT AS IDENTITY" in ddl
    for table_name, keys in EXECUTION_UNIQUE_KEYS.items():
        table = cast(Table, ENVIRONMENT_MODELS[table_name].__table__)
        indexes = {str(index.name): index for index in table.indexes}
        for key in keys:
            index = indexes[f"uq_{table_name}_{key}"]
            assert index.unique
            ddl = str(CreateIndex(index).compile(dialect=pg_dialect()))
            assert "(environment, namespace," in ddl
            assert f"payload ->> '{key}'" in ddl
            assert "IS NOT NULL" in ddl
    assert "sentinel_events" not in EXECUTION_UNIQUE_KEYS
    assert "orders" not in EXECUTION_UNIQUE_KEYS


def test_upgrade_covers_existing_schemas_without_deleting_audit_evidence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    specification = spec_from_file_location(
        "durable_execution_migration",
        Path(__file__).parents[2] / "migrations" / "versions" / "0002_durable_execution.py",
    )
    assert specification is not None and specification.loader is not None
    migration = module_from_spec(specification)
    specification.loader.exec_module(migration)
    statements: list[str] = []

    class Connection:
        def execute(self, statement: Any) -> None:
            statements.append(str(statement))

    monkeypatch.setattr(migration.op, "get_bind", lambda: Connection())
    migration.upgrade()

    alterations = [statement for statement in statements if statement.startswith("ALTER TABLE")]
    assert len(alterations) == len(SHARED_TABLE_NAMES) + 2 * len(ENVIRONMENT_TABLE_NAMES)
    assert all("ADD COLUMN IF NOT EXISTS append_sequence" in statement for statement in alterations)
    assert any('"demo"."order_intents"' in statement for statement in statements)
    assert any('"live"."order_intents"' in statement for statement in statements)
    assert all("DELETE" not in statement and "DROP" not in statement for statement in statements)
    assert any("CREATE UNIQUE INDEX" in statement for statement in statements)
