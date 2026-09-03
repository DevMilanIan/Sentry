from __future__ import annotations

import hashlib
from datetime import datetime
from uuid import UUID, uuid4

from pydantic import Field, field_validator

from app.domain.models import TimestampedModel


class SourceDocument(TimestampedModel):
    document_id: UUID = Field(default_factory=uuid4)
    source_id: str
    canonical_url: str
    title: str
    normalized_text: str
    publication_time: datetime | None = None
    fetched_at: datetime
    tickers: tuple[str, ...] = ()
    untrusted_external_text: bool = True

    @field_validator("title", "normalized_text")
    @classmethod
    def no_control_characters(cls, value: str) -> str:
        return "".join(
            character for character in value if character in "\n\t" or ord(character) >= 32
        )

    @property
    def content_hash(self) -> str:
        normalized = " ".join(self.normalized_text.lower().split())
        return hashlib.sha256(normalized.encode("utf-8")).hexdigest()

    @property
    def deduplication_key(self) -> str:
        normalized_title = " ".join(self.title.lower().split())
        raw = f"{self.source_id}\n{self.canonical_url}\n{normalized_title}\n{self.content_hash}"
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()
