from __future__ import annotations

import asyncio
import builtins
from collections import defaultdict
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any, cast
from uuid import UUID, uuid4

from pydantic import BaseModel, TypeAdapter
from sqlalchemy import select

from app.config import RuntimeBinding
from app.db.models import ENVIRONMENT_MODELS, SHARED_MODELS
from app.db.session import DatabaseManager
from app.exceptions import SafetyCriticalError

_JSON_MAPPING_ADAPTER = TypeAdapter(dict[str, Any])


def _json_payload(value: BaseModel | Mapping[str, Any]) -> dict[str, Any]:
    if isinstance(value, BaseModel):
        return value.model_dump(mode="json")
    return cast(
        dict[str, Any],
        _JSON_MAPPING_ADAPTER.dump_python(dict(value), mode="json"),
    )


def _extract_created_at(payload: Mapping[str, Any]) -> datetime:
    value = payload.get("created_at")
    if isinstance(value, datetime):
        result = value
    elif isinstance(value, str):
        result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    else:
        result = datetime.now(UTC)
    if result.tzinfo is None:
        raise ValueError("persisted timestamps must be timezone-aware")
    return result.astimezone(UTC)


def _extract_run_id(payload: Mapping[str, Any]) -> UUID | None:
    value = payload.get("run_id")
    if not value:
        return None
    return value if isinstance(value, UUID) else UUID(str(value))


class InMemoryAuditRepository:
    """Deterministic test/offline repository bound to one environment namespace."""

    def __init__(self, binding: RuntimeBinding) -> None:
        self.binding = binding
        self._rows: dict[str, list[dict[str, Any]]] = defaultdict(list)
        self._lock = asyncio.Lock()
        self.writable = True

    def _guard(self, payload: Mapping[str, Any]) -> None:
        payload_environment = payload.get("environment")
        if payload_environment is not None:
            value = getattr(payload_environment, "value", payload_environment)
            if value != self.binding.environment.value:
                raise SafetyCriticalError("cross-environment persistence attempt")
        if payload.get("namespace", self.binding.idempotency_namespace) != (
            self.binding.idempotency_namespace
        ):
            raise SafetyCriticalError("cross-namespace persistence attempt")

    async def append(self, table: str, value: BaseModel | Mapping[str, Any]) -> UUID:
        if not self.writable:
            raise SafetyCriticalError("repository is not writable")
        if table not in SHARED_MODELS and table not in ENVIRONMENT_MODELS:
            raise ValueError(f"unknown audit table: {table}")
        payload = _json_payload(value)
        if table in ENVIRONMENT_MODELS:
            self._guard(payload)
        row_id = uuid4()
        row = {
            "id": row_id,
            "created_at": _extract_created_at(payload),
            "run_id": _extract_run_id(payload),
            "record_type": type(value).__name__,
            "environment": self.binding.environment.value if table in ENVIRONMENT_MODELS else None,
            "namespace": self.binding.idempotency_namespace
            if table in ENVIRONMENT_MODELS
            else None,
            "payload": payload,
        }
        async with self._lock:
            row["append_sequence"] = len(self._rows[table]) + 1
            self._rows[table].append(row)
        return row_id

    async def list(self, table: str, *, limit: int = 100) -> list[dict[str, Any]]:
        async with self._lock:
            return [dict(row) for row in self._rows.get(table, [])[-limit:]]

    async def find_payload(self, table: str, key: str, value: Any) -> dict[str, Any] | None:
        rows = await self.list_payloads(table, filters={key: value}, limit=1)
        return rows[0] if rows else None

    async def list_payloads(
        self,
        table: str,
        *,
        filters: Mapping[str, Any] | None = None,
        limit: int = 1000,
        before_sequence: int | None = None,
    ) -> builtins.list[dict[str, Any]]:
        if table not in SHARED_MODELS and table not in ENVIRONMENT_MODELS:
            raise ValueError(f"unknown audit table: {table}")
        if not 1 <= limit <= 10_000:
            raise ValueError("audit query limit must be between 1 and 10000")
        normalized = _json_payload(filters or {})
        async with self._lock:
            return [
                dict(row)
                for row in reversed(self._rows.get(table, []))
                if (before_sequence is None or row["append_sequence"] < before_sequence)
                and all(row["payload"].get(key) == value for key, value in normalized.items())
                and (
                    table not in ENVIRONMENT_MODELS
                    or (
                        row["environment"] == self.binding.environment.value
                        and row["namespace"] == self.binding.idempotency_namespace
                    )
                )
            ][:limit]

    async def healthcheck(self) -> bool:
        return self.writable


class PostgresAuditRepository:
    def __init__(self, manager: DatabaseManager) -> None:
        self.manager = manager
        self.binding = manager.binding

    def _model_for(self, table: str) -> type[Any]:
        try:
            return ENVIRONMENT_MODELS.get(table) or SHARED_MODELS[table]
        except KeyError as exc:
            raise ValueError(f"unknown audit table: {table}") from exc

    def _guard(self, table: str, payload: Mapping[str, Any]) -> None:
        if table not in ENVIRONMENT_MODELS:
            return
        payload_environment = payload.get("environment")
        if payload_environment is not None:
            value = getattr(payload_environment, "value", payload_environment)
            if value != self.binding.environment.value:
                raise SafetyCriticalError("cross-environment persistence attempt")
        if payload.get("namespace", self.binding.idempotency_namespace) != (
            self.binding.idempotency_namespace
        ):
            raise SafetyCriticalError("cross-namespace persistence attempt")

    async def append(self, table: str, value: BaseModel | Mapping[str, Any]) -> UUID:
        payload = _json_payload(value)
        self._guard(table, payload)
        model = self._model_for(table)
        row_id = uuid4()
        values: dict[str, Any] = {
            "id": row_id,
            "created_at": _extract_created_at(payload),
            "run_id": _extract_run_id(payload),
            "record_type": type(value).__name__,
            "payload": payload,
        }
        if table in ENVIRONMENT_MODELS:
            values.update(
                environment=self.binding.environment.value,
                namespace=self.binding.idempotency_namespace,
            )
        async with self.manager.session() as session:
            session.add(model(**values))
        return row_id

    async def list(self, table: str, *, limit: int = 100) -> list[dict[str, Any]]:
        return await self.list_payloads(table, limit=limit)

    async def find_payload(self, table: str, key: str, value: Any) -> dict[str, Any] | None:
        rows = await self.list_payloads(table, filters={key: value}, limit=1)
        return rows[0] if rows else None

    async def list_payloads(
        self,
        table: str,
        *,
        filters: Mapping[str, Any] | None = None,
        limit: int = 1000,
        before_sequence: int | None = None,
    ) -> builtins.list[dict[str, Any]]:
        """Read one bound namespace in durable insertion order, with keyset pagination."""
        if not 1 <= limit <= 10_000:
            raise ValueError("audit query limit must be between 1 and 10000")
        model = self._model_for(table)
        statement = select(model)
        if table in ENVIRONMENT_MODELS:
            statement = statement.where(
                model.environment == self.binding.environment.value,
                model.namespace == self.binding.idempotency_namespace,
            )
        for key, value in _json_payload(filters or {}).items():
            if isinstance(value, str) or value is None:
                comparison = model.payload[key].as_string() == value
            elif isinstance(value, bool):
                comparison = model.payload[key].as_boolean() == value
            elif isinstance(value, int):
                comparison = model.payload[key].as_integer() == value
            elif isinstance(value, float):
                comparison = model.payload[key].as_float() == value
            else:
                raise ValueError("audit payload filters must be scalar values")
            statement = statement.where(comparison)
        if before_sequence is not None:
            statement = statement.where(model.append_sequence < before_sequence)
        statement = statement.order_by(model.append_sequence.desc(), model.id.desc()).limit(limit)
        async with self.manager.session() as session:
            rows = (await session.scalars(statement)).all()
        return [
            {
                "id": row.id,
                "append_sequence": row.append_sequence,
                "created_at": row.created_at,
                "run_id": row.run_id,
                "record_type": row.record_type,
                "environment": getattr(row, "environment", None),
                "namespace": getattr(row, "namespace", None),
                "payload": row.payload,
            }
            for row in rows
        ]

    async def healthcheck(self) -> bool:
        return await self.manager.healthcheck()
