from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from typing import Literal
from urllib.parse import urlsplit
from uuid import UUID, uuid4

from pydantic import Field, field_validator, model_validator

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
    untrusted_external_text: Literal[True] = True
    data_mode: Literal["FIXTURE", "LIVE_READ", "REPLAY"] = "FIXTURE"
    stored_content_hash: str | None = Field(default=None, pattern=r"^[a-f0-9]{64}$")

    @model_validator(mode="after")
    def verify_stored_hash(self) -> SourceDocument:
        if self.stored_content_hash is not None and self.stored_content_hash != self.content_hash:
            raise ValueError("stored source content hash does not match normalized content")
        return self

    @field_validator("fetched_at", "publication_time")
    @classmethod
    def aware_source_time(cls, value: datetime | None) -> datetime | None:
        if value is None:
            return value
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("source timestamps must be timezone-aware")
        return value.astimezone(UTC)

    @field_validator("canonical_url")
    @classmethod
    def safe_reference_url(cls, value: str) -> str:
        parsed = urlsplit(value)
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
        ):
            raise ValueError("source references require credential-free HTTP(S) URLs")
        return value

    @field_validator("title", "normalized_text")
    @classmethod
    def no_control_characters(cls, value: str) -> str:
        return "".join(
            character for character in value if character in "\n\t" or ord(character) >= 32
        )

    @property
    def content_hash(self) -> str:
        # Include the headline: many legal feeds provide no summary at all.
        normalized = " ".join(f"{self.title}\n{self.normalized_text}".lower().split())
        return hashlib.sha256(normalized.encode("utf-8")).hexdigest()

    @property
    def deduplication_key(self) -> str:
        normalized_title = " ".join(self.title.lower().split())
        raw = f"{self.source_id}\n{self.canonical_url}\n{normalized_title}\n{self.content_hash}"
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()
