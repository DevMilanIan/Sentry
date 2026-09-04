"""Opt-in shared-reference tests using only the existing fresh, owned schema fixture."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from test_postgres_live import PostgresTestDatabase
from test_postgres_live import postgres_database as _postgres_database

from app.clock.base import VirtualClock
from app.db.repository import PostgresAuditRepository
from app.federal.registry import RelationshipType
from app.federal.service import (
    EvidenceStatus,
    FederalRegistryService,
    RegistryConflictError,
    RelationshipDraft,
)

pytestmark = pytest.mark.integration
postgres_database = _postgres_database


def synthetic_draft(instant: datetime, *, notes: str = "") -> RelationshipDraft:
    return RelationshipDraft(
        ticker="TEST",
        issuer_name="Synthetic Test Company",
        agency="Synthetic Agency",
        relationship_type=RelationshipType.STRATEGIC_INVESTMENT,
        announcement_date=instant.date(),
        primary_source_url="https://energy.gov/synthetic-registry-test-not-a-factual-record",
        source_publication_date=instant.date(),
        source_available_at=instant,
        last_verified_at=instant,
        confidence=Decimal("0.9"),
        notes=notes,
    )


async def test_postgres_federal_registry_serializes_competing_connection_pools(
    postgres_database: PostgresTestDatabase,
) -> None:
    instant = datetime(2026, 9, 4, tzinfo=UTC)
    clock = VirtualClock(instant)
    repositories = [PostgresAuditRepository(postgres_database.manager()) for _ in range(2)]
    services = [FederalRegistryService(repository, clock) for repository in repositories]
    initial = await services[0].create(synthetic_draft(instant), actor="test", reason="initial")
    results = await asyncio.gather(
        *(
            service.revise(
                initial.relationship_id,
                synthetic_draft(instant, notes=f"competing edit {index}"),
                expected_revision_id=initial.revision_id,
                actor="test",
                reason="concurrency test",
            )
            for index, service in enumerate(services)
        ),
        return_exceptions=True,
    )
    assert sum(isinstance(result, RegistryConflictError) for result in results) == 1
    assert sum(not isinstance(result, BaseException) for result in results) == 1
    history = await services[0].history(initial.relationship_id)
    assert len(history.revisions) == 2
    assert history.revisions[0].previous_revision_id == initial.revision_id
    assert len((await services[1].snapshot()).items) == 1


async def test_postgres_federal_registry_restart_preserves_causal_history_and_score(
    postgres_database: PostgresTestDatabase,
) -> None:
    instant = datetime(2026, 9, 4, tzinfo=UTC)
    clock = VirtualClock(instant)
    first = FederalRegistryService(PostgresAuditRepository(postgres_database.manager()), clock)
    initial = await first.create(synthetic_draft(instant), actor="test", reason="initial")
    await clock.advance(timedelta(hours=1))
    inactive = synthetic_draft(instant).model_copy(update={"active": False})
    updated = await first.revise(
        initial.relationship_id,
        inactive,
        expected_revision_id=initial.revision_id,
        actor="test",
        reason="withdrawal test",
    )
    fresh = FederalRegistryService(PostgresAuditRepository(postgres_database.manager()), clock)
    assert (await fresh.snapshot(as_of=instant)).items[0].revision == initial
    assert (await fresh.snapshot()).items[0].revision == updated
    assert (await fresh.snapshot()).items[0].evidence_status is EvidenceStatus.INACTIVE
    assert (await fresh.score("TEST", as_of=instant)).score.value == Decimal("73.80")
    assert (await fresh.score("TEST")).score.value == 0
    assert len((await fresh.history(initial.relationship_id)).revisions) == 2
