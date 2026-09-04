from __future__ import annotations

import asyncio
import html
import re
from collections.abc import Iterable
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import httpx
from defusedxml import ElementTree as ET
from defusedxml.common import DefusedXmlException
from pydantic import ValidationError

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
        return None  # An absent source timezone is unknown, not assumed UTC.
    return result.astimezone(UTC)


class FeedDocumentParser:
    """Minimal legal RSS/Atom parser with deterministic normalization."""

    def parse(self, source_id: str, body: bytes, fetched_at: datetime) -> list[SourceDocument]:
        try:
            root = ET.fromstring(body)
        except (ET.ParseError, DefusedXmlException) as exc:
            raise DataInvalidError(f"invalid XML from {source_id}") from exc
        if root.tag not in {"rss", "{http://www.w3.org/2005/Atom}feed"}:
            raise DataInvalidError(f"source {source_id} is not a supported RSS/Atom feed")
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
        maximum_response_bytes: int = 2_000_000,
        maximum_documents: int = 1000,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        if timeout_seconds <= 0 or maximum_response_bytes <= 0 or maximum_documents <= 0:
            raise ValueError("collector timeout and budgets must be positive")
        if "example.invalid" in user_agent:
            self._contact_configured = False
        else:
            self._contact_configured = True
        self._clock = clock
        self._user_agent = user_agent
        self._parser = parser or FeedDocumentParser()
        self._timeout = timeout_seconds
        self._maximum_response_bytes = maximum_response_bytes
        self._maximum_documents = maximum_documents
        self._transport = transport

    async def fetch(
        self, source_id: str, url: str, *, sec_source: bool = False
    ) -> list[SourceDocument]:
        if sec_source and not self._contact_configured:
            raise DataInvalidError("SEC collector requires a real contact in its User-Agent")
        parsed_url = urlsplit(url)
        if (
            parsed_url.scheme != "https"
            or not parsed_url.hostname
            or parsed_url.username is not None
            or parsed_url.password is not None
        ):
            raise DataInvalidError("official source URL must be credential-free HTTPS")
        headers = {
            "User-Agent": self._user_agent,
            "Accept": "application/rss+xml, application/atom+xml, application/xml",
        }
        try:
            async with asyncio.timeout(self._timeout), httpx.AsyncClient(
                timeout=self._timeout,
                follow_redirects=False,
                headers=headers,
                trust_env=False,
                transport=self._transport,
            ) as client:
                async with client.stream("GET", url) as response:
                    response.raise_for_status()
                    chunks: list[bytes] = []
                    size = 0
                    async for chunk in response.aiter_bytes():
                        size += len(chunk)
                        if size > self._maximum_response_bytes:
                            raise DataInvalidError("official source response exceeds byte budget")
                        chunks.append(chunk)
        except (TimeoutError, httpx.TransportError) as exc:
            raise TransientError(f"source {source_id} unavailable") from exc
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code in {408, 429, 500, 502, 503, 504}:
                raise TransientError(
                    f"source {source_id} returned {exc.response.status_code}"
                ) from exc
            raise DataInvalidError(
                f"source {source_id} returned {exc.response.status_code}"
            ) from exc
        try:
            documents = self._parser.parse(source_id, b"".join(chunks), self._clock.now())
        except ValidationError as exc:
            raise DataInvalidError(f"invalid source document from {source_id}") from exc
        if len(documents) > self._maximum_documents:
            raise DataInvalidError("official source response exceeds document budget")
        return documents


def deduplicate_documents(documents: Iterable[SourceDocument]) -> list[SourceDocument]:
    seen_hashes: set[tuple[str, str]] = set()
    result: list[SourceDocument] = []
    ordered = sorted(
        documents,
        key=lambda document: (
            document.publication_time or document.fetched_at,
            document.source_id,
            document.canonical_url,
            document.deduplication_key,
        ),
    )
    for document in ordered:
        identity = (document.source_id, document.content_hash)
        if identity in seen_hashes:
            continue
        # Preserve conflicting revisions at one URL rather than randomly choosing
        # a winner, and retain identical text from independent primary sources.
        seen_hashes.add(identity)
        result.append(document)
    return result
