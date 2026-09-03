from __future__ import annotations

import html
import re
from collections.abc import Iterable
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import httpx
from defusedxml import ElementTree as ET

from app.catalysts.models import SourceDocument
from app.clock.base import Clock
from app.exceptions import DataInvalidError, TransientError

_TAG = re.compile(r"<[^>]+>")
_SPACE = re.compile(r"\s+")
_TRACKING_PREFIXES = ("utm_", "mc_", "ref")


def canonicalize_url(value: str) -> str:
    parts = urlsplit(value.strip())
    query = [
        (key, item)
        for key, item in parse_qsl(parts.query, keep_blank_values=True)
        if not key.lower().startswith(_TRACKING_PREFIXES)
    ]
    return urlunsplit(
        (parts.scheme.lower(), parts.netloc.lower(), parts.path.rstrip("/"), urlencode(query), "")
    )


def normalize_external_text(value: str, maximum_length: int = 50_000) -> str:
    """Flatten active markup; the result remains explicitly untrusted data."""
    without_tags = _TAG.sub(" ", html.unescape(value))
    return _SPACE.sub(" ", without_tags).strip()[:maximum_length]


def _parse_time(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        result = parsedate_to_datetime(value)
    except (TypeError, ValueError):
        try:
            result = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
    if result.tzinfo is None:
        result = result.replace(tzinfo=UTC)
    return result.astimezone(UTC)


class FeedDocumentParser:
    """Minimal legal RSS/Atom parser with deterministic normalization."""

    def parse(self, source_id: str, body: bytes, fetched_at: datetime) -> list[SourceDocument]:
        try:
            root = ET.fromstring(body)
        except ET.ParseError as exc:
            raise DataInvalidError(f"invalid XML from {source_id}: {exc}") from exc
        items = list(root.findall(".//item"))
        atom_namespace = "{http://www.w3.org/2005/Atom}"
        if not items:
            items = list(root.findall(f".//{atom_namespace}entry"))
        results: list[SourceDocument] = []
        for item in items:
            if item.tag.startswith(atom_namespace):
                title = item.findtext(f"{atom_namespace}title") or ""
                summary = (
                    item.findtext(f"{atom_namespace}summary")
                    or item.findtext(f"{atom_namespace}content")
                    or ""
                )
                link_node = item.find(f"{atom_namespace}link")
                link = link_node.get("href", "") if link_node is not None else ""
                published = item.findtext(f"{atom_namespace}published") or item.findtext(
                    f"{atom_namespace}updated"
                )
            else:
                title = item.findtext("title") or ""
                summary = (
                    item.findtext("description")
                    or item.findtext("{http://purl.org/rss/1.0/modules/content/}encoded")
                    or ""
                )
                link = item.findtext("link") or ""
                published = item.findtext("pubDate") or item.findtext("date")
            if not title.strip() or not link.strip():
                continue
            results.append(
                SourceDocument(
                    created_at=fetched_at,
                    source_id=source_id,
                    canonical_url=canonicalize_url(link),
                    title=normalize_external_text(title, 1_000),
                    normalized_text=normalize_external_text(summary),
                    publication_time=_parse_time(published),
                    fetched_at=fetched_at,
                )
            )
        return results


class OfficialSourceCollector:
    def __init__(
        self,
        clock: Clock,
        *,
        user_agent: str,
        parser: FeedDocumentParser | None = None,
        timeout_seconds: float = 20.0,
    ) -> None:
        if "example.invalid" in user_agent:
            self._contact_configured = False
        else:
            self._contact_configured = True
        self._clock = clock
        self._user_agent = user_agent
        self._parser = parser or FeedDocumentParser()
        self._timeout = timeout_seconds

    async def fetch(
        self, source_id: str, url: str, *, sec_source: bool = False
    ) -> list[SourceDocument]:
        if sec_source and not self._contact_configured:
            raise DataInvalidError("SEC collector requires a real contact in its User-Agent")
        headers = {
            "User-Agent": self._user_agent,
            "Accept": "application/rss+xml, application/atom+xml, application/xml",
        }
        try:
            async with httpx.AsyncClient(
                timeout=self._timeout, follow_redirects=True, headers=headers
            ) as client:
                response = await client.get(url)
                response.raise_for_status()
        except (httpx.TimeoutException, httpx.NetworkError) as exc:
            raise TransientError(f"source {source_id} unavailable") from exc
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code in {408, 429, 500, 502, 503, 504}:
                raise TransientError(
                    f"source {source_id} returned {exc.response.status_code}"
                ) from exc
            raise DataInvalidError(
                f"source {source_id} returned {exc.response.status_code}"
            ) from exc
        return self._parser.parse(source_id, response.content, self._clock.now())


def deduplicate_documents(documents: Iterable[SourceDocument]) -> list[SourceDocument]:
    seen_urls: set[str] = set()
    seen_hashes: set[str] = set()
    result: list[SourceDocument] = []
    ordered = sorted(
        documents,
        key=lambda document: (
            document.publication_time or document.fetched_at,
            str(document.document_id),
        ),
    )
    for document in ordered:
        if document.canonical_url in seen_urls or document.content_hash in seen_hashes:
            continue
        seen_urls.add(document.canonical_url)
        seen_hashes.add(document.content_hash)
        result.append(document)
    return result
