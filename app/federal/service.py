from __future__ import annotations

import asyncio
import hashlib
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Any, Literal
from urllib.parse import urlsplit
from uuid import UUID, uuid4
from weakref import WeakKeyDictionary

from pydantic import Field, ValidationError, field_validator
from sqlalchemy import select, text

from app.clock.base import Clock
from app.db.models import SHARED_MODELS
from app.db.repository import InMemoryAuditRepository, PostgresAuditRepository
from app.domain.models import DomainModel, TimestampedModel, sha256_json
from app.federal.registry import (
    ExposureScore,
    FederalExposureScorer,
    FederalRelationship,
    FederalRelationshipDetails,
)

RegistryRepository = InMemoryAuditRepository | PostgresAuditRepository
TABLE = "federal_relationships"
_MEMORY_LOCKS: WeakKeyDictionary[InMemoryAuditRepository, asyncio.Lock] = WeakKeyDictionary()
DEFAULT_PRIMARY_HOSTS = frozenset(
    {
        "whitehouse.gov",
        "defense.gov",
        "dod.gov",
        "energy.gov",
        "commerce.gov",
        "treasury.gov",
        "sec.gov",
        "usaspending.gov",
        "sam.gov",
        "grants.gov",
        "federalregister.gov",
        "gao.gov",
        "congress.gov",
        "nasa.gov",
        "doi.gov",
    }
)


class RegistryConflictError(ValueError):
    pass


class RegistryIntegrityError(RuntimeError):
    pass


class RegistryLimitError(RuntimeError):
    pass


class RegistryNotFoundError(LookupError):
    pass


class RelationshipDraft(FederalRelationshipDetails):
    source_available_at: datetime


class RegistryPolicy(DomainModel):
    version: str = "federal-reference-policy-v1"
    maximum_verification_age_days: int = Field(default=90, ge=1, le=365)
    maximum_scan_records: int = Field(default=10_000, ge=1, le=100_000)
    approved_primary_hosts: frozenset[str] = DEFAULT_PRIMARY_HOSTS

    @field_validator("approved_primary_hosts")
    @classmethod
    def explicit_dns_hosts(cls, hosts: frozenset[str]) -> frozenset[str]:
        import re

        if (
            not hosts
            or len(hosts) > 100
            or any(
                len(host) > 253 or not re.fullmatch(r"[a-z0-9]+(?:[.-][a-z0-9]+)*\.[a-z]{2,}", host)
                for host in hosts
            )
        ):
            raise ValueError("reference policy requires bounded explicit DNS hostnames")
        return hosts

    def accepts_source(self, url: str) -> bool:
        host = urlsplit(url).hostname or ""
        return any(
            host == approved or host.endswith("." + approved)
            for approved in self.approved_primary_hosts
        )


class RegistryRevision(TimestampedModel):
    registry_kind: Literal["federal-registry-revision-v1"] = "federal-registry-revision-v1"
    revision_id: UUID
    relationship_id: UUID
    previous_revision_id: UUID | None
    actor: str = Field(min_length=1, max_length=120)
    reason: str = Field(min_length=1, max_length=2000)
    relationship: FederalRelationship
    revision_hash: str = Field(pattern=r"^[a-f0-9]{64}$")


class EvidenceStatus(StrEnum):
    VERIFIED_REFERENCE = "VERIFIED_REFERENCE"
    UNVERIFIED = "UNVERIFIED"
    STALE = "STALE"
    INACTIVE = "INACTIVE"
    ENDED = "ENDED"
    NOT_EFFECTIVE = "NOT_EFFECTIVE"
    SOURCE_NOT_APPROVED = "SOURCE_NOT_APPROVED"


class RegistryItem(DomainModel):
    revision: RegistryRevision
    evidence_status: EvidenceStatus
    eligible_for_research_score: bool


class RegistryPage(DomainModel):
    as_of: datetime
    items: tuple[RegistryItem, ...]
    next_after_relationship_id: UUID | None
    total_relationships: int
    policy_version: str
    research_only: Literal[True] = True


class RevisionPage(DomainModel):
    revisions: tuple[RegistryRevision, ...]
    next_before_sequence: int | None


class RegistryScore(DomainModel):
    ticker: str
    as_of: datetime
    score: ExposureScore
    included_relationship_ids: tuple[UUID, ...]
    excluded_relationship_ids: tuple[UUID, ...]
    policy_version: str
    research_only: Literal[True] = True


def _decode(payload: Mapping[str, Any]) -> RegistryRevision:
    try:
        revision = RegistryRevision.model_validate(payload)
        digest_payload = revision.model_dump(mode="json", exclude={"revision_hash"})
        if (
            sha256_json(digest_payload) != revision.revision_hash
            or revision.relationship_id != revision.relationship.relationship_id
            or revision.relationship.created_at > revision.created_at
            or revision.relationship.source_available_at is None
            or revision.relationship.source_available_at > revision.created_at
            or (
                revision.relationship.last_verified_at is not None
                and revision.relationship.last_verified_at > revision.created_at
            )
        ):
            raise ValueError("invalid revision identity, availability, or digest")
        return revision
    except (ValueError, ValidationError) as error:
        raise RegistryIntegrityError(
            "stored federal revision failed integrity validation"
        ) from error


def _assert_parent(payload: Mapping[str, Any] | None, revision: RegistryRevision) -> None:
    previous = _decode(payload) if payload is not None else None
    if (previous.revision_id if previous else None) != revision.previous_revision_id:
        raise RegistryConflictError(
            "relationship changed; reload its latest revision before editing"
        )
    if previous and (
        previous.created_at > revision.created_at
        or previous.relationship.created_at != revision.relationship.created_at
    ):
        raise RegistryConflictError("revision clock or original relationship timestamp changed")


async def _atomic_append(repository: RegistryRepository, revision: RegistryRevision) -> None:
    """Parent comparison and append are one locked PostgreSQL transaction, not a read/write CAS."""
    if isinstance(repository, InMemoryAuditRepository):
        lock = _MEMORY_LOCKS.setdefault(repository, asyncio.Lock())
        async with lock:
            rows = await repository.list_payloads(
                TABLE, filters={"relationship_id": str(revision.relationship_id)}, limit=1
            )
            _assert_parent(rows[0]["payload"] if rows else None, revision)
            await repository.append(TABLE, revision)
        return
    if not isinstance(repository, PostgresAuditRepository):
        raise TypeError("registry writes require a supported atomic repository")
    model: Any = SHARED_MODELS[TABLE]
    lock_key = int.from_bytes(
        hashlib.sha256(f"sentinel.federal-registry:{revision.relationship_id}".encode()).digest()[
            :8
        ],
        "big",
        signed=True,
    )
    async with repository.manager.session() as session:
        await session.execute(text("SET LOCAL lock_timeout = '5s'"))
        await session.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": lock_key})
        statement = (
            select(model)
            .where(model.payload["relationship_id"].as_string() == str(revision.relationship_id))
            .order_by(model.append_sequence.desc())
            .limit(1)
        )
        previous = await session.scalar(statement)
        _assert_parent(previous.payload if previous is not None else None, revision)
        session.add(
            model(
                id=uuid4(),
                created_at=revision.created_at,
                run_id=None,
                record_type=type(revision).__name__,
                payload=revision.model_dump(mode="json"),
            )
        )


class FederalRegistryService:
    """Audited reference data only. This service has no broker, order, or hard-risk interfaces."""

    def __init__(
        self,
        repository: RegistryRepository,
        wall_clock: Clock,
        *,
        policy: RegistryPolicy | None = None,
    ) -> None:
        self.repository = repository
        self.wall_clock = wall_clock
        self.policy = policy or RegistryPolicy()

    def _as_of(self, requested: datetime | None) -> datetime:
        now = self.wall_clock.now()
        result = requested or now
        if result.tzinfo is None or result.utcoffset() is None or result > now:
            raise ValueError("as_of must be timezone-aware and cannot be in the future")
        return result.astimezone(UTC)

    async def _all_revisions(self, relationship_id: UUID | None = None) -> list[RegistryRevision]:
        revisions: list[RegistryRevision] = []
        before: int | None = None
        filters = {"relationship_id": str(relationship_id)} if relationship_id else None
        while True:
            remaining = self.policy.maximum_scan_records - len(revisions)
            rows = await self.repository.list_payloads(
                TABLE, filters=filters, before_sequence=before, limit=min(500, remaining + 1)
            )
            if len(rows) > remaining:
                raise RegistryLimitError(
                    "registry scan limit exceeded; narrow the query or revise its explicit limit"
                )
            revisions.extend(_decode(row["payload"]) for row in rows)
            if not rows or len(rows) < min(500, remaining + 1):
                return revisions
            cursor = rows[-1]["append_sequence"]
            if (
                not isinstance(cursor, int)
                or cursor <= 0
                or (before is not None and cursor >= before)
            ):
                raise RegistryIntegrityError("registry pagination did not advance")
            before = cursor

    async def create(
        self, draft: RelationshipDraft, *, actor: str, reason: str
    ) -> RegistryRevision:
        return await self._write(uuid4(), draft, actor=actor, reason=reason, previous=None)

    async def revise(
        self,
        relationship_id: UUID,
        draft: RelationshipDraft,
        *,
        expected_revision_id: UUID,
        actor: str,
        reason: str,
    ) -> RegistryRevision:
        rows = await self._all_revisions(relationship_id)
        if not rows:
            raise RegistryNotFoundError("federal relationship does not exist")
        self._validate_chain(rows)
        previous = rows[0]
        if previous.revision_id != expected_revision_id:
            raise RegistryConflictError(
                "relationship changed; reload its latest revision before editing"
            )
        return await self._write(
            relationship_id, draft, actor=actor, reason=reason, previous=previous
        )

    async def _write(
        self,
        relationship_id: UUID,
        draft: RelationshipDraft,
        *,
        actor: str,
        reason: str,
        previous: RegistryRevision | None,
    ) -> RegistryRevision:
        draft = RelationshipDraft.model_validate(draft.model_dump())
        now = self._as_of(None)
        if not actor.strip() or not reason.strip():
            raise ValueError("actor and revision reason must not be blank")
        if not self.policy.accepts_source(draft.primary_source_url):
            raise ValueError("primary source host is not approved by the reference policy")
        if (
            draft.source_available_at > now
            or draft.source_publication_date > draft.source_available_at.date()
            or draft.announcement_date > draft.source_available_at.date()
            or (
                draft.last_verified_at is not None
                and (
                    draft.last_verified_at > now
                    or draft.last_verified_at < draft.source_available_at
                )
            )
        ):
            raise ValueError(
                "source availability, publication, announcement, or verification is not causal"
            )
        relationship = FederalRelationship(
            **draft.model_dump(),
            relationship_id=relationship_id,
            created_at=previous.relationship.created_at if previous else now,
        )
        fields: dict[str, Any] = {
            "created_at": now,
            "registry_kind": "federal-registry-revision-v1",
            "revision_id": uuid4(),
            "relationship_id": relationship_id,
            "previous_revision_id": previous.revision_id if previous else None,
            "actor": actor,
            "reason": reason,
            "relationship": relationship,
        }
        provisional = RegistryRevision(**fields, revision_hash="0" * 64)
        revision = provisional.model_copy(
            update={
                "revision_hash": sha256_json(
                    provisional.model_dump(mode="json", exclude={"revision_hash"})
                )
            }
        )
        await _atomic_append(self.repository, revision)
        return revision

    @staticmethod
    def _validate_chain(revisions: list[RegistryRevision]) -> None:
        # Repository rows are newest insertion first; same-time edits still have a stable order.
        latest: dict[UUID, RegistryRevision] = {}
        seen: set[UUID] = set()
        for revision in reversed(revisions):
            previous = latest.get(revision.relationship_id)
            if (
                revision.revision_id in seen
                or revision.previous_revision_id != (previous.revision_id if previous else None)
                or (previous and revision.created_at < previous.created_at)
            ):
                raise RegistryIntegrityError(
                    "registry revision chain is incomplete, duplicated, or branched"
                )
            latest[revision.relationship_id] = revision
            seen.add(revision.revision_id)

    def _item(self, revision: RegistryRevision, as_of: datetime) -> RegistryItem:
        relationship = revision.relationship
        if not relationship.active:
            evidence = EvidenceStatus.INACTIVE
        elif relationship.end_date and relationship.end_date < as_of.date():
            evidence = EvidenceStatus.ENDED
        elif relationship.effective_date and relationship.effective_date > as_of.date():
            evidence = EvidenceStatus.NOT_EFFECTIVE
        elif not self.policy.accepts_source(relationship.primary_source_url):
            evidence = EvidenceStatus.SOURCE_NOT_APPROVED
        elif relationship.last_verified_at is None:
            evidence = EvidenceStatus.UNVERIFIED
        elif as_of - relationship.last_verified_at > timedelta(
            days=self.policy.maximum_verification_age_days
        ):
            evidence = EvidenceStatus.STALE
        else:
            evidence = EvidenceStatus.VERIFIED_REFERENCE
        return RegistryItem(
            revision=revision,
            evidence_status=evidence,
            eligible_for_research_score=evidence is EvidenceStatus.VERIFIED_REFERENCE,
        )

    async def snapshot(
        self,
        *,
        as_of: datetime | None = None,
        ticker: str | None = None,
        limit: int = 100,
        after_relationship_id: UUID | None = None,
    ) -> RegistryPage:
        instant = self._as_of(as_of)
        if not 1 <= limit <= 200:
            raise ValueError("page limit must be between 1 and 200")
        revisions = await self._all_revisions()
        self._validate_chain(revisions)
        latest: dict[UUID, RegistryRevision] = {}
        for revision in reversed(revisions):
            # Even a backdated source claim was not known until this server-recorded revision.
            if revision.created_at <= instant:
                latest[revision.relationship_id] = revision
        items = [
            self._item(revision, instant)
            for revision in latest.values()
            if ticker is None or revision.relationship.ticker == ticker
        ]
        items.sort(key=lambda item: str(item.revision.relationship_id))
        total = len(items)
        if after_relationship_id is not None:
            items = [
                item
                for item in items
                if str(item.revision.relationship_id) > str(after_relationship_id)
            ]
        page = items[:limit]
        return RegistryPage(
            as_of=instant,
            items=tuple(page),
            total_relationships=total,
            next_after_relationship_id=page[-1].revision.relationship_id
            if len(items) > limit
            else None,
            policy_version=self.policy.version,
        )

    async def history(
        self, relationship_id: UUID, *, limit: int = 100, before_sequence: int | None = None
    ) -> RevisionPage:
        if not 1 <= limit <= 200 or (before_sequence is not None and before_sequence <= 0):
            raise ValueError("history limit or cursor is outside its allowed range")
        rows = await self.repository.list_payloads(
            TABLE,
            filters={"relationship_id": str(relationship_id)},
            limit=limit + 1,
            before_sequence=before_sequence,
        )
        revisions = tuple(_decode(row["payload"]) for row in rows[:limit])
        return RevisionPage(
            revisions=revisions,
            next_before_sequence=rows[limit - 1]["append_sequence"] if len(rows) > limit else None,
        )

    async def score(self, ticker: str, *, as_of: datetime | None = None) -> RegistryScore:
        instant = self._as_of(as_of)
        items: list[RegistryItem] = []
        cursor: UUID | None = None
        while True:
            page = await self.snapshot(
                as_of=instant, ticker=ticker, limit=200, after_relationship_id=cursor
            )
            items.extend(page.items)
            cursor = page.next_after_relationship_id
            if cursor is None:
                break
        included = [item for item in items if item.eligible_for_research_score]
        return RegistryScore(
            ticker=ticker,
            as_of=instant,
            score=FederalExposureScorer().score([item.revision.relationship for item in included]),
            included_relationship_ids=tuple(item.revision.relationship_id for item in included),
            excluded_relationship_ids=tuple(
                item.revision.relationship_id
                for item in items
                if not item.eligible_for_research_score
            ),
            policy_version=self.policy.version,
        )
