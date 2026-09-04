from __future__ import annotations

import asyncio

import httpx
import pytest

from app.catalysts.collector import OfficialSourceCollector
from app.clock.base import VirtualClock
from app.exceptions import DataInvalidError, TransientError


@pytest.mark.parametrize(
    "body,limit,match",
    [
        (b"<html><body>Denied</body></html>", 1000, "RSS/Atom"),
        (b"x" * 1025, 1024, "byte budget"),
    ],
)
async def test_collector_does_not_accept_nonfeed_or_unbounded_response(
    clock: VirtualClock,
    body: bytes,
    limit: int,
    match: str,
) -> None:
    collector = OfficialSourceCollector(
        clock,
        user_agent="OptionsSentinel fixture",
        maximum_response_bytes=limit,
        transport=httpx.MockTransport(lambda request: httpx.Response(200, content=body)),
    )
    with pytest.raises(DataInvalidError, match=match):
        await collector.fetch("fixture", "https://agency.gov/feed")


async def test_sec_contact_guard_and_redirect_deny_precede_other_network_reads(
    clock: VirtualClock,
) -> None:
    calls: list[str] = []

    def response(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        return httpx.Response(302, headers={"location": "https://different.gov/feed"})

    collector = OfficialSourceCollector(
        clock,
        user_agent="OptionsSentinel contact@example.invalid",
        transport=httpx.MockTransport(response),
    )
    with pytest.raises(DataInvalidError, match="real contact"):
        await collector.fetch("sec", "https://www.sec.gov/feed", sec_source=True)
    assert not calls
    with pytest.raises(DataInvalidError, match="302"):
        await collector.fetch("fixture", "https://agency.gov/feed")
    assert calls == ["https://agency.gov/feed"]


async def test_trickling_feed_has_total_wall_time_bound(clock: VirtualClock) -> None:
    class TricklingStream(httpx.AsyncByteStream):
        async def __aiter__(self):  # type: ignore[no-untyped-def]
            while True:
                await asyncio.sleep(0.005)
                yield b" "

    collector = OfficialSourceCollector(
        clock,
        user_agent="OptionsSentinel fixture",
        timeout_seconds=0.03,
        transport=httpx.MockTransport(
            lambda request: httpx.Response(200, stream=TricklingStream())
        ),
    )
    with pytest.raises(TransientError, match="unavailable"):
        async with asyncio.timeout(1):
            await collector.fetch("fixture", "https://agency.gov/feed")


async def test_collector_negotiates_official_text_xml_without_changing_identity(
    clock: VirtualClock,
) -> None:
    def response(request: httpx.Request) -> httpx.Response:
        assert request.headers["User-Agent"] == "OptionsSentinel fixture"
        accepted = {value.strip() for value in request.headers["Accept"].split(",")}
        if "text/xml" not in accepted:
            return httpx.Response(406)
        return httpx.Response(
            200,
            headers={"Content-Type": "text/xml; charset=UTF-8"},
            content=(
                b"<rss><channel><item><title>Official release</title>"
                b"<link>https://agency.gov/release</link>"
                b"<pubDate>Thu, 03 Sep 2026 12:00:00 GMT</pubDate>"
                b"</item></channel></rss>"
            ),
        )

    collector = OfficialSourceCollector(
        clock,
        user_agent="OptionsSentinel fixture",
        transport=httpx.MockTransport(response),
    )
    documents = await collector.fetch("fixture", "https://agency.gov/feed")
    assert len(documents) == 1
    assert documents[0].publication_time is not None
    assert documents[0].canonical_url == "https://agency.gov/release"
