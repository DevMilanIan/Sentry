from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

import pytest

from app.clock.base import VirtualClock
from app.exceptions import DataInvalidError
from app.reasoning import (
    BoundedDeepResearchWorker,
    DeepResearchLimits,
    DeepResearchStatus,
    ReasoningRole,
    ResearchClaim,
    ResearchDocument,
    ResearchSynthesis,
    ScriptedReplayModelProvider,
)

NOW = datetime(2026, 9, 3, 15, 0, tzinfo=UTC)


@dataclass(slots=True)
class StaticResearchSource:
    source_id: str
    documents: tuple[ResearchDocument, ...] = ()
    searches: list[tuple[str, int]] = field(default_factory=list)

    async def search(self, query: str, *, limit: int) -> tuple[ResearchDocument, ...]:
        self.searches.append((query, limit))
        # Deliberately return the configured fixture verbatim. This lets the
        # cap test prove the worker enforces its own global bound even if an
        # adapter returns more rows than requested.
        return self.documents


def document(
    document_id: str,
    *,
    source_id: str = "sec-filings",
    observed_at: datetime = NOW,
    effective_at: datetime | None = None,
    url: str | None = None,
    excerpt: str | None = None,
) -> ResearchDocument:
    return ResearchDocument(
        document_id=document_id,
        source_id=source_id,
        title=f"Document {document_id}",
        url=url or f"https://example.test/{document_id}",
        effective_at=effective_at or observed_at,
        observed_at=observed_at,
        excerpt=excerpt or f"Verified evidence from {document_id}.",
    )


def synthesis(*document_ids: str) -> ResearchSynthesis:
    return ResearchSynthesis(
        summary="Approved evidence supports a bounded conclusion.",
        claims=(
            ResearchClaim(
                claim="The disclosed event is material to the stated research question.",
                document_ids=tuple(document_ids),
            ),
        ),
        counterevidence=(),
        uncertainties=("Market response remains uncertain.",),
        research_needed=(),
        abstain_reason=None,
    )


def scripted_provider(*document_ids: str) -> ScriptedReplayModelProvider:
    return ScriptedReplayModelProvider(
        {ReasoningRole.DEEP_RESEARCH: synthesis(*document_ids)},
        script_version="deep-research-test-v1",
    )


@pytest.mark.asyncio
async def test_successful_scripted_synthesis_is_clock_deterministic_and_called_once() -> None:
    source_document = document(
        "filing-1",
        observed_at=NOW - timedelta(minutes=1),
        effective_at=NOW - timedelta(minutes=2),
    )

    async def run_once() -> tuple[object, ScriptedReplayModelProvider, StaticResearchSource]:
        provider = scripted_provider(source_document.document_id)
        source = StaticResearchSource("sec-filings", (source_document,))
        result = await BoundedDeepResearchWorker(
            VirtualClock(NOW),
            provider,
            (source,),
            limits=DeepResearchLimits(max_model_calls=1),
        ).run("  ACME   material event  ")
        return result, provider, source

    first, first_provider, first_source = await run_once()
    second, second_provider, _ = await run_once()

    assert first.status is DeepResearchStatus.COMPLETE
    assert first.query == "ACME material event"
    assert first.started_at == NOW
    assert first.completed_at == NOW
    assert first.source_documents == (source_document,)
    assert first.synthesis is not None
    assert first.synthesis.claims[0].document_ids == (source_document.document_id,)
    assert first.model_calls == 1
    assert first_provider.calls == [ReasoningRole.DEEP_RESEARCH]
    assert second_provider.calls == [ReasoningRole.DEEP_RESEARCH]
    assert first_source.searches == [("ACME material event", 12)]
    assert first.run_hash == second.run_hash


@pytest.mark.asyncio
async def test_source_documents_are_deduplicated_then_capped_deterministically() -> None:
    effective_at = NOW - timedelta(minutes=10)
    original = document(
        "doc-0",
        observed_at=NOW - timedelta(minutes=5),
        effective_at=effective_at,
        url="https://example.test/shared",
        excerpt="The same canonical evidence.",
    )
    duplicate = document(
        "doc-0-duplicate",
        observed_at=NOW - timedelta(minutes=4),
        effective_at=effective_at,
        url=original.url,
        excerpt=original.excerpt,
    )
    unique_1 = document("doc-1", observed_at=NOW - timedelta(minutes=3))
    unique_2 = document("doc-2", observed_at=NOW - timedelta(minutes=2))
    unique_3 = document("doc-3", observed_at=NOW - timedelta(minutes=1))
    source = StaticResearchSource(
        "sec-filings",
        (unique_3, duplicate, unique_2, original, unique_1),
    )
    provider = scripted_provider("doc-0")

    result = await BoundedDeepResearchWorker(
        VirtualClock(NOW),
        provider,
        (source,),
        limits=DeepResearchLimits(max_model_calls=1, max_source_documents=3),
    ).run("ACME evidence")

    assert result.status is DeepResearchStatus.COMPLETE
    assert source.searches == [("ACME evidence", 3)]
    assert tuple(item.document_id for item in result.source_documents) == (
        "doc-0",
        "doc-1",
        "doc-2",
    )
    assert len({item.content_hash for item in result.source_documents}) == 3
    assert provider.calls == [ReasoningRole.DEEP_RESEARCH]


@pytest.mark.asyncio
@pytest.mark.parametrize("invalid_kind", ["future", "unapproved_source"])
async def test_future_or_unapproved_source_documents_are_rejected(invalid_kind: str) -> None:
    if invalid_kind == "future":
        invalid = document(
            "future-1",
            observed_at=NOW + timedelta(seconds=1),
            effective_at=NOW + timedelta(seconds=1),
        )
    else:
        invalid = document("rogue-1", source_id="unapproved-adapter")
    source = StaticResearchSource("sec-filings", (invalid,))
    provider = scripted_provider(invalid.document_id)
    worker = BoundedDeepResearchWorker(VirtualClock(NOW), provider, (source,))

    with pytest.raises(DataInvalidError):
        await worker.run("ACME evidence")

    assert provider.calls == []


@pytest.mark.asyncio
async def test_no_documents_abstains_without_calling_the_model() -> None:
    source = StaticResearchSource("sec-filings")
    provider = scripted_provider("unused-document")

    result = await BoundedDeepResearchWorker(
        VirtualClock(NOW),
        provider,
        (source,),
    ).run("ACME unavailable evidence")

    assert result.status is DeepResearchStatus.ABSTAINED
    assert result.started_at == NOW
    assert result.completed_at == NOW
    assert result.source_documents == ()
    assert result.model_calls == 0
    assert result.model_name is None
    assert result.synthesis is not None
    assert result.synthesis.claims == ()
    assert result.synthesis.abstain_reason == "no approved source evidence"
    assert provider.calls == []

