from __future__ import annotations

from datetime import datetime, timedelta

import httpx
import pytest

from app.catalysts.collector import (
    FeedDocumentParser,
    OfficialSourceCollector,
    deduplicate_documents,
)
from app.catalysts.models import SourceDocument
from app.catalysts.runtime import CatalystIngestionWorker
from app.clock.base import VirtualClock
from app.config import OfficialSourceConfig, RuntimeBinding, SourcesConfig
from app.db.repository import InMemoryAuditRepository
from app.domain.enums import DemoBackend
from app.exceptions import DataInvalidError, SafetyCriticalError

FEED = b"""<rss><channel><item><title>Agency release</title>
<link>https://agency.gov/release</link><description>External source text</description>
<pubDate>Tue, 01 Sep 2026 13:55:00 GMT</pubDate></item></channel></rss>"""


def setup_worker(
    clock: VirtualClock,
    binding: RuntimeBinding,
    body: bytes = FEED,
    *,
    repository: InMemoryAuditRepository | None = None,
) -> tuple[CatalystIngestionWorker, InMemoryAuditRepository, list[str]]:
    audit = repository or InMemoryAuditRepository(
        binding.model_copy(update={"demo_backend": DemoBackend.BROKER_SHADOW})
    )
    calls: list[str] = []

    def respond(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        return httpx.Response(200, content=body)

    collector = OfficialSourceCollector(
        clock, user_agent="OptionsSentinel fixture", transport=httpx.MockTransport(respond)
    )
    config = SourcesConfig(
        version="fixture",
        sec_user_agent="OptionsSentinel fixture",
        poll_seconds=900,
        official_sources=(
            OfficialSourceConfig(id="disabled", url="https://disabled.gov/feed"),
            OfficialSourceConfig(id="agency", url="https://agency.gov/feed", enabled=True),
        ),
    )
    return CatalystIngestionWorker(config, clock, audit, collector), audit, calls


async def test_polls_enabled_source_and_deduplicates_across_restart(
    clock: VirtualClock, demo_binding: RuntimeBinding
) -> None:
    worker, audit, calls = setup_worker(clock, demo_binding)
    await worker.poll()
    await clock.advance(timedelta(minutes=15))
    restarted, _, _ = setup_worker(clock, demo_binding, repository=audit)
    await restarted.poll()
    docs = await audit.list("source_documents")
    events = await audit.list("sentinel_events")
    assert calls == ["https://agency.gov/feed"]
    assert len(docs) == len(events) == 1
    doc = SourceDocument.model_validate(docs[0]["payload"])
    assert doc.data_mode == "LIVE_READ" and doc.untrusted_external_text
    assert doc.stored_content_hash == doc.content_hash
    assert events[0]["payload"]["effective_at"] == doc.fetched_at.isoformat().replace("+00:00", "Z")
    assert events[0]["payload"]["raw_reference_ids"] == [str(doc.document_id)]
    assert not events[0]["payload"]["tickers"]  # No invented entity/ticker association.


async def test_document_to_event_crash_gap_is_repaired_without_new_document(
    clock: VirtualClock, demo_binding: RuntimeBinding
) -> None:
    class FailedEventRepository(InMemoryAuditRepository):
        fail_event = True

        async def append(self, table, value):  # type: ignore[no-untyped-def]
            if table == "sentinel_events" and self.fail_event:
                raise SafetyCriticalError("fixture event commit failure")
            return await super().append(table, value)

    audit = FailedEventRepository(
        demo_binding.model_copy(update={"demo_backend": DemoBackend.BROKER_SHADOW})
    )
    worker, _, _ = setup_worker(clock, demo_binding, repository=audit)
    with pytest.raises(SafetyCriticalError, match="event commit"):
        await worker.poll()
    assert len(await audit.list("source_documents")) == 1
    assert not await audit.list("sentinel_events")
    audit.fail_event = False
    restarted, _, _ = setup_worker(
        clock, demo_binding, b"<rss><channel/></rss>", repository=audit
    )
    await restarted.poll()  # The item is no longer present in the remote feed.
    assert len(await audit.list("source_documents")) == 1
    assert len(await audit.list("sentinel_events")) == 1


async def test_malformed_item_is_audited_as_feed_failure(
    clock: VirtualClock, demo_binding: RuntimeBinding
) -> None:
    worker, audit, _ = setup_worker(
        clock, demo_binding, FEED.replace(b"https://agency.gov/release", b"javascript:alert(1)")
    )
    await worker.poll()
    status = (await audit.list("health_events"))[0]["payload"]
    assert not status["healthy"] and status["failure_type"] == "DataInvalidError"
    assert not await audit.list("source_documents")


@pytest.mark.parametrize(
    "publication",
    [b"", b"Tue, 01 Sep 2026 14:01:00 GMT", b"Tue, 18 Aug 2026 13:55:00 GMT"],
)
async def test_unknown_future_or_old_publications_do_not_emit_current_catalyst(
    clock: VirtualClock, demo_binding: RuntimeBinding, publication: bytes
) -> None:
    body = FEED.replace(b"Tue, 01 Sep 2026 13:55:00 GMT", publication)
    worker, audit, _ = setup_worker(clock, demo_binding, body)
    await worker.poll()
    assert len(await audit.list("source_documents")) == 1
    assert not await audit.list("sentinel_events")


async def test_read_failures_are_audited_and_database_failure_prevents_network(
    clock: VirtualClock, demo_binding: RuntimeBinding
) -> None:
    worker, audit, calls = setup_worker(clock, demo_binding, b"<html>Denied</html>")
    await worker.poll()
    status = (await audit.list("health_events"))[0]["payload"]
    assert status["healthy"] is False and status["failure_type"] == "DataInvalidError"
    audit.writable = False
    with pytest.raises(SafetyCriticalError, match="writable audit"):
        await worker.poll()
    assert len(calls) == 1


def test_offline_worker_cannot_poll_current_sources(
    clock: VirtualClock, demo_binding: RuntimeBinding
) -> None:
    with pytest.raises(SafetyCriticalError, match="OFFLINE_SIM"):
        setup_worker(clock, demo_binding, repository=InMemoryAuditRepository(demo_binding))


def test_source_document_rejects_unsafe_provenance(instant: datetime) -> None:
    document = FeedDocumentParser().parse("fixture", FEED, instant)[0]
    for updates in (
        {"fetched_at": instant.replace(tzinfo=None)},
        {"publication_time": instant.replace(tzinfo=None)},
        {"canonical_url": "javascript:alert(1)"},
        {"canonical_url": "https://:secret@agency.gov/path"},
        {"untrusted_external_text": False},
    ):
        with pytest.raises(ValueError):
            SourceDocument.model_validate({**document.model_dump(), **updates})


def test_empty_summaries_have_distinct_headline_hashes(instant: datetime) -> None:
    first = FeedDocumentParser().parse("fixture", FEED, instant)[0]
    first = first.model_copy(update={"normalized_text": ""})
    second = first.model_copy(update={"title": "Different announcement"})
    assert first.content_hash != second.content_hash


def test_dedup_preserves_revisions_and_primary_source_corroboration(instant: datetime) -> None:
    parser = FeedDocumentParser()
    first = parser.parse("fixture", FEED, instant)[0]
    revision = first.model_copy(update={"title": "Corrected announcement"})
    corroboration = first.model_copy(update={"source_id": "independent"})
    expected = deduplicate_documents([first, revision, first, corroboration])
    permuted = deduplicate_documents([corroboration, revision, first, first])
    assert len(expected) == 3
    assert [doc.deduplication_key for doc in expected] == [
        doc.deduplication_key for doc in permuted
    ]


def test_xml_entities_are_rejected_as_data_errors(instant: datetime) -> None:
    body = b'<!DOCTYPE rss [<!ENTITY x "unsafe">]><rss><channel>&x;</channel></rss>'
    with pytest.raises(DataInvalidError, match="invalid XML"):
        FeedDocumentParser().parse("fixture", body, instant)
