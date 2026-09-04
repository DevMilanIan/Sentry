from __future__ import annotations

import asyncio
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any
from uuid import uuid4

import pytest
from fastapi import FastAPI, Header, HTTPException
from fastapi.testclient import TestClient
from pydantic import ValidationError

from app.clock.base import VirtualClock
from app.config import RuntimeBinding
from app.db.repository import InMemoryAuditRepository
from app.federal.api import create_federal_registry_router
from app.federal.registry import RelationshipType
from app.federal.service import (
    EvidenceStatus,
    FederalRegistryService,
    RegistryConflictError,
    RegistryIntegrityError,
    RegistryLimitError,
    RegistryPolicy,
    RelationshipDraft,
)


def draft(instant: datetime, **changes: Any) -> RelationshipDraft:
    fields = {
        "ticker": "TEST",
        "issuer_name": "Synthetic Test Company",
        "agency": "Test Agency",
        "relationship_type": RelationshipType.STRATEGIC_INVESTMENT,
        "announcement_date": instant.date(),
        "source_publication_date": instant.date(),
        "primary_source_url": "https://www.energy.gov/synthetic-test-not-a-real-record",
        "source_available_at": instant,
        "last_verified_at": instant,
        "confidence": Decimal("0.9"),
    }
    fields.update(changes)
    return RelationshipDraft.model_validate(fields)


@pytest.mark.parametrize(
    "url",
    [
        "http://energy.gov/example",
        "https://user:password@energy.gov/example",
        "https://energy.gov:8443/example",
        "https://energy.gov/a#fragment",
        "https://energy.gov/a\n",
        "https://energy.gov\\@evil.test/a",
        "https://énergy.gov/a",
    ],
)
def test_primary_uri_rejects_unsafe_syntax(instant: datetime, url: str) -> None:
    with pytest.raises(ValueError):
        draft(instant, primary_source_url=url)


@pytest.mark.parametrize(
    "url",
    [
        "https://energy.gov.evil.test/a",
        "https://127.0.0.1/a",
        "https://localhost/a",
        "https://example.test/a",
        "https://fake-energy.gov/a",
    ],
)
async def test_unapproved_hosts_never_write(
    demo_binding: RuntimeBinding, clock: VirtualClock, url: str
) -> None:
    repo = InMemoryAuditRepository(demo_binding)
    service = FederalRegistryService(repo, clock)
    with pytest.raises(ValueError, match="host is not approved"):
        await service.create(
            draft(clock.now(), primary_source_url=url), actor="operator", reason="test"
        )
    assert await repo.list("federal_relationships") == []


def test_draft_rejects_backdated_server_fields_and_unbounded_values(instant: datetime) -> None:
    for changes in (
        {"created_at": instant},
        {"relationship_id": uuid4()},
        {"recorded_at": instant},
        {"ticker": "lowercase"},
        {"notes": "x" * 4001},
        {"active": "true"},
        {"last_verified_at": instant.replace(tzinfo=None)},
        {"confidence": Decimal("0.0000001")},
        {"financing_amount": Decimal("1.000000001")},
    ):
        with pytest.raises(ValidationError):
            draft(instant, **changes)


async def test_append_only_revision_history_is_causal_and_retains_original_identity(
    demo_binding: RuntimeBinding, clock: VirtualClock
) -> None:
    service = FederalRegistryService(InMemoryAuditRepository(demo_binding), clock)
    start = clock.now()
    first = await service.create(draft(start), actor="operator", reason="initial evidence review")
    await clock.advance(timedelta(hours=1))
    second = await service.revise(
        first.relationship_id,
        draft(start, active=False),
        expected_revision_id=first.revision_id,
        actor="operator",
        reason="relationship withdrawn",
    )
    assert second.relationship.created_at == first.relationship.created_at == start
    assert second.relationship_id == first.relationship_id
    assert second.previous_revision_id == first.revision_id
    assert second.created_at == clock.now()
    assert (await service.snapshot(as_of=start)).items[0].revision == first
    assert (await service.snapshot()).items[0].evidence_status is EvidenceStatus.INACTIVE
    assert (await service.snapshot(as_of=start - timedelta(seconds=1))).items == ()
    history = await service.history(first.relationship_id, limit=1)
    assert history.revisions == (second,)
    assert history.next_before_sequence is not None
    previous = await service.history(
        first.relationship_id, before_sequence=history.next_before_sequence
    )
    assert previous.revisions == (first,)
    assert first.actor == "operator" and first.reason == "initial evidence review"


async def test_delayed_entry_cannot_make_old_source_visible_before_recording(
    demo_binding: RuntimeBinding, clock: VirtualClock
) -> None:
    service = FederalRegistryService(InMemoryAuditRepository(demo_binding), clock)
    old = clock.now() - timedelta(days=2)
    await service.create(draft(old), actor="operator", reason="late manual review")
    assert (await service.snapshot(as_of=old)).items == ()
    assert len((await service.snapshot()).items) == 1
    with pytest.raises(ValueError, match="future"):
        await service.snapshot(as_of=clock.now() + timedelta(seconds=1))


async def test_invalid_causality_and_blank_audit_rejected(
    demo_binding: RuntimeBinding, clock: VirtualClock
) -> None:
    repo = InMemoryAuditRepository(demo_binding)
    service = FederalRegistryService(repo, clock)
    future = clock.now() + timedelta(seconds=1)
    for changes in (
        {"last_verified_at": future},
        {"source_available_at": future},
        {"source_publication_date": clock.now().date() + timedelta(days=1)},
        {"last_verified_at": clock.now() - timedelta(seconds=1)},
    ):
        with pytest.raises(ValueError, match="causal"):
            await service.create(draft(clock.now(), **changes), actor="operator", reason="test")
    for actor, reason in ((" ", "test"), ("operator", " ")):
        with pytest.raises(ValueError, match="blank"):
            await service.create(draft(clock.now()), actor=actor, reason=reason)
    assert await repo.list("federal_relationships") == []


@pytest.mark.parametrize(
    ("changes", "status"),
    [
        ({"active": False}, EvidenceStatus.INACTIVE),
        ({"last_verified_at": None}, EvidenceStatus.UNVERIFIED),
        ({"effective_date": "2027-01-01"}, EvidenceStatus.NOT_EFFECTIVE),
        ({"end_date": "2026-08-31"}, EvidenceStatus.ENDED),
    ],
)
async def test_ineligible_evidence_is_labeled_and_excluded_from_score(
    demo_binding: RuntimeBinding,
    clock: VirtualClock,
    changes: dict[str, Any],
    status: EvidenceStatus,
) -> None:
    service = FederalRegistryService(InMemoryAuditRepository(demo_binding), clock)
    await service.create(draft(clock.now(), **changes), actor="operator", reason="test")
    assert (await service.snapshot()).items[0].evidence_status is status
    assert (await service.score("TEST")).score.value == 0


async def test_staleness_preserves_deterministic_original_scorer(
    demo_binding: RuntimeBinding, clock: VirtualClock
) -> None:
    service = FederalRegistryService(InMemoryAuditRepository(demo_binding), clock)
    await service.create(draft(clock.now()), actor="operator", reason="test")
    scored = await service.score("TEST")
    assert scored.score.value == Decimal("73.80")
    assert scored.research_only and scored.score.version == "federal-exposure-v1"
    await clock.advance(timedelta(days=91))
    assert (await service.snapshot()).items[0].evidence_status is EvidenceStatus.STALE
    assert (await service.score("TEST")).score.value == 0


async def test_concurrent_writers_accept_only_one_expected_parent(
    demo_binding: RuntimeBinding, clock: VirtualClock
) -> None:
    repo = InMemoryAuditRepository(demo_binding)
    services = [FederalRegistryService(repo, clock), FederalRegistryService(repo, clock)]
    first = await services[0].create(draft(clock.now()), actor="operator", reason="test")
    results = await asyncio.gather(
        *(
            service.revise(
                first.relationship_id,
                draft(clock.now(), notes=f"edit {index}"),
                expected_revision_id=first.revision_id,
                actor="operator",
                reason="test",
            )
            for index, service in enumerate(services)
        ),
        return_exceptions=True,
    )
    assert sum(isinstance(result, RegistryConflictError) for result in results) == 1
    assert len(await repo.list("federal_relationships")) == 2
    assert len((await services[0].snapshot()).items) == 1


async def test_snapshot_pagination_and_explicit_scan_limit(
    demo_binding: RuntimeBinding, clock: VirtualClock
) -> None:
    repo = InMemoryAuditRepository(demo_binding)
    service = FederalRegistryService(repo, clock)
    for _ in range(3):
        await service.create(draft(clock.now()), actor="operator", reason="test")
    first = await service.snapshot(limit=2)
    assert len(first.items) == 2 and first.total_relationships == 3
    second = await service.snapshot(
        as_of=first.as_of, limit=2, after_relationship_id=first.next_after_relationship_id
    )
    assert len(second.items) == 1 and second.next_after_relationship_id is None
    bounded = FederalRegistryService(repo, clock, policy=RegistryPolicy(maximum_scan_records=2))
    with pytest.raises(RegistryLimitError, match="scan limit"):
        await bounded.snapshot()


async def test_stored_payload_corruption_fails_closed(
    demo_binding: RuntimeBinding, clock: VirtualClock
) -> None:
    repo = InMemoryAuditRepository(demo_binding)
    service = FederalRegistryService(repo, clock)
    await service.create(draft(clock.now()), actor="operator", reason="test")
    repo._rows["federal_relationships"][0]["payload"]["reason"] = "tampered"
    with pytest.raises(RegistryIntegrityError):
        await service.snapshot()


def test_router_authentication_and_server_actor_and_timestamp(
    demo_binding: RuntimeBinding, clock: VirtualClock
) -> None:
    repo = InMemoryAuditRepository(demo_binding)

    async def authorize(x_dashboard_token: str = Header(default="")) -> None:
        if x_dashboard_token != "test-only":
            raise HTTPException(401)

    app = FastAPI()
    app.include_router(
        create_federal_registry_router(repo, clock, authorize, actor="authenticated-local")
    )
    with TestClient(app) as client:
        for route in (
            "/relationships",
            "/policy",
            "/score/TEST",
            f"/relationships/{uuid4()}/history",
        ):
            assert client.get("/api/federal" + route).status_code == 401
        body = {"relationship": draft(clock.now()).model_dump(mode="json"), "reason": "test"}
        assert client.post("/api/federal/relationships", json=body).status_code == 401
        headers = {"x-dashboard-token": "test-only"}
        assert (
            client.post(
                "/api/federal/relationships", json={**body, "actor": "forged"}, headers=headers
            ).status_code
            == 422
        )
        created = client.post("/api/federal/relationships", json=body, headers=headers)
        assert created.status_code == 201
        record = created.json()
        assert record["actor"] == "authenticated-local"
        update = {**body, "expected_revision_id": str(uuid4())}
        assert (
            client.put(
                f"/api/federal/relationships/{record['relationship_id']}",
                json=update,
                headers=headers,
            ).status_code
            == 409
        )
        assert (
            client.get("/api/federal/relationships?limit=201", headers=headers).status_code == 422
        )
