from __future__ import annotations

from collections.abc import Awaitable, Callable
from datetime import datetime
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import Field

from app.clock.base import Clock
from app.domain.models import DomainModel
from app.federal.service import (
    FederalRegistryService,
    RegistryConflictError,
    RegistryIntegrityError,
    RegistryLimitError,
    RegistryNotFoundError,
    RegistryPage,
    RegistryPolicy,
    RegistryRepository,
    RegistryRevision,
    RegistryScore,
    RelationshipDraft,
    RevisionPage,
)


class CreateRelationshipRequest(DomainModel):
    relationship: RelationshipDraft
    reason: str = Field(min_length=1, max_length=2000)


class ReviseRelationshipRequest(CreateRelationshipRequest):
    expected_revision_id: UUID


async def _response[T](operation: Awaitable[T]) -> T:
    try:
        return await operation
    except RegistryConflictError as error:
        raise HTTPException(409, str(error)) from error
    except RegistryNotFoundError as error:
        raise HTTPException(404, str(error)) from error
    except (RegistryIntegrityError, RegistryLimitError) as error:
        raise HTTPException(503, str(error)) from error
    except ValueError as error:
        raise HTTPException(422, "invalid registry query or noncausal reference data") from error


def create_federal_registry_router(
    repository: RegistryRepository,
    wall_clock: Clock,
    authorize: Callable[..., Awaitable[None]],
    *,
    actor: str = "local-dashboard",
    policy: RegistryPolicy | None = None,
) -> APIRouter:
    """The injected authorization dependency protects reads and writes. No public write route."""
    service = FederalRegistryService(repository, wall_clock, policy=policy)
    router = APIRouter(prefix="/api/federal", dependencies=[Depends(authorize)])

    @router.get("/relationships")
    async def relationships(
        as_of: datetime | None = None,
        ticker: str | None = Query(default=None, pattern=r"^[A-Z][A-Z0-9.-]{0,14}$"),
        limit: int = Query(default=100, ge=1, le=200),
        after_relationship_id: UUID | None = None,
    ) -> RegistryPage:
        return await _response(
            service.snapshot(
                as_of=as_of, ticker=ticker, limit=limit, after_relationship_id=after_relationship_id
            )
        )

    @router.post("/relationships", status_code=201)
    async def create(request: CreateRelationshipRequest) -> RegistryRevision:
        return await _response(
            service.create(request.relationship, actor=actor, reason=request.reason)
        )

    @router.put("/relationships/{relationship_id}")
    async def revise(relationship_id: UUID, request: ReviseRelationshipRequest) -> RegistryRevision:
        return await _response(
            service.revise(
                relationship_id,
                request.relationship,
                expected_revision_id=request.expected_revision_id,
                actor=actor,
                reason=request.reason,
            )
        )

    @router.get("/relationships/{relationship_id}/history")
    async def history(
        relationship_id: UUID,
        limit: int = Query(default=100, ge=1, le=200),
        before_sequence: int | None = Query(default=None, ge=1),
    ) -> RevisionPage:
        return await _response(
            service.history(relationship_id, limit=limit, before_sequence=before_sequence)
        )

    @router.get("/score/{ticker}")
    async def score(ticker: str, as_of: datetime | None = None) -> RegistryScore:
        return await _response(service.score(ticker, as_of=as_of))

    @router.get("/policy")
    async def reference_policy() -> RegistryPolicy:
        return service.policy

    return router
