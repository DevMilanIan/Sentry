from __future__ import annotations

import asyncio
from datetime import timedelta
from uuid import NAMESPACE_URL, uuid5

from app.catalysts.collector import OfficialSourceCollector, deduplicate_documents
from app.catalysts.models import SourceDocument
from app.clock.base import Clock
from app.config import SourcesConfig
from app.db.repository import InMemoryAuditRepository, PostgresAuditRepository
from app.domain.enums import DemoBackend
from app.domain.models import SentinelEvent
from app.exceptions import DataInvalidError, SafetyCriticalError, TransientError


class CatalystIngestionWorker:
    """Bounded public-feed ingestion; no inference, broker access, or trade authority.

    One process owns a runtime namespace. A document is committed before its stable
    event identity; interrupted polls repair that gap on retry. Shared document
    inserts can duplicate across independent environments, but identical IDs and
    environment-scoped events preserve correlation. This is not an exactly-once
    cross-process queue or a replacement for the configured source access policy.
    """

    def __init__(
        self,
        config: SourcesConfig,
        clock: Clock,
        repository: InMemoryAuditRepository | PostgresAuditRepository,
        collector: OfficialSourceCollector,
        *,
        maximum_event_age: timedelta = timedelta(days=7),
    ) -> None:
        if repository.binding.demo_backend is DemoBackend.OFFLINE_SIM:
            raise SafetyCriticalError("live source polling is forbidden in OFFLINE_SIM")
        if maximum_event_age <= timedelta(0):
            raise ValueError("maximum event age must be positive")
        self.config = config
        self.clock = clock
        self.repository = repository
        self.collector = collector
        self.maximum_event_age = maximum_event_age
        self._lock = asyncio.Lock()

    async def poll(self) -> None:
        async with self._lock:
            if not await self.repository.healthcheck():
                raise SafetyCriticalError("source ingestion requires writable audit storage")
            repaired = await self._repair_events()
            for source in self.config.official_sources:
                if not source.enabled:
                    continue
                started = self.clock.now()
                failure: str | None = None
                try:
                    documents = await self.collector.fetch(
                        source.id, source.url, sec_source=source.id == "sec"
                    )
                except (DataInvalidError, TransientError) as exc:
                    # Persist the class, never arbitrary server responses or exception text.
                    failure = type(exc).__name__
                    documents = []
                created = 0
                for document in deduplicate_documents(documents):
                    if await self._persist(document):
                        created += 1
                await self.repository.append(
                    "health_events",
                    {
                        "created_at": self.clock.now(),
                        "component": "official_source",
                        "source_id": source.id,
                        "started_at": started,
                        "healthy": failure is None,
                        "failure_type": failure,
                        "documents_received": len(documents),
                        "new_events": created,
                        "repaired_events": repaired.get(source.id, 0),
                        "data_mode": "LIVE_READ",
                        "source_config_version": self.config.version,
                    },
                )

    async def _repair_events(self) -> dict[str, int]:
        """Repair the durable document/event gap even after a feed rolls past it."""
        enabled = {source.id for source in self.config.official_sources if source.enabled}
        repaired: dict[str, int] = {}
        before: int | None = None
        for _ in range(100):
            rows = await self.repository.list_payloads(
                "source_documents",
                filters={"data_mode": "LIVE_READ"},
                limit=1000,
                before_sequence=before,
            )
            for row in rows:
                document = SourceDocument.model_validate(row["payload"])
                if document.source_id in enabled and await self._persist(document):
                    repaired[document.source_id] = repaired.get(document.source_id, 0) + 1
            if len(rows) < 1000:
                return repaired
            before = min(row["append_sequence"] for row in rows)
        raise SafetyCriticalError("source outbox scan exceeds durable recovery bound")

    async def _persist(self, document: SourceDocument) -> bool:
        now = self.clock.now()
        if document.fetched_at > now:
            raise DataInvalidError("source fetch timestamp lies in the future")
        document_id = uuid5(NAMESPACE_URL, f"official-source-v2:{document.deduplication_key}")
        existing = await self.repository.find_payload(
            "source_documents", "document_id", str(document_id)
        )
        if existing is None:
            document = document.model_copy(
                update={
                    "document_id": document_id,
                    "data_mode": "LIVE_READ",
                    "stored_content_hash": document.content_hash,
                }
            )
            await self.repository.append("source_documents", document)
        else:
            # Preserve the earliest durable observation, not the most recent fetch.
            document = SourceDocument.model_validate(existing["payload"])
        publication = document.publication_time
        # Retain unknown/stale/future-dated material for inspection but do not wake
        # decision workers with a current-catalyst claim that lacks temporal proof.
        if publication is None or not timedelta(0) <= now - publication <= self.maximum_event_age:
            return False
        event_id = uuid5(
            NAMESPACE_URL,
            f"official-source-event:{self.repository.binding.idempotency_namespace}:{document_id}",
        )
        if await self.repository.find_payload("sentinel_events", "event_id", str(event_id)):
            return False
        event = SentinelEvent(
            created_at=now,
            event_id=event_id,
            event_type="official_source_document",
            source=document.source_id,
            # Knowledge becomes available when fetched, never retroactively at publication.
            effective_at=document.fetched_at,
            tickers=document.tickers,
            severity=1,
            deduplication_key=document.deduplication_key,
            raw_reference_ids=(str(document_id),),
            payload={
                "data_mode": "LIVE_READ",
                "publication_time": publication.isoformat(),
                "content_hash": document.content_hash,
                "untrusted_external_text": True,
            },
        )
        await self.repository.append("sentinel_events", event)
        return True
