from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal

from app.catalysts.collector import FeedDocumentParser, canonicalize_url, deduplicate_documents
from app.federal.registry import FederalExposureScorer, FederalRelationship, RelationshipType


def test_feed_parser_normalizes_and_deduplicates_markup() -> None:
    body = b"""<rss><channel><item><title> Strategic &amp; award </title>
    <link>https://agency.gov/release?id=1&amp;utm_source=x</link>
    <description><![CDATA[<script>ignore()</script><b>Company wins award</b>]]></description>
    <pubDate>Tue, 01 Sep 2026 12:00:00 GMT</pubDate></item></channel></rss>"""
    instant = datetime(2026, 9, 1, 12, 1, tzinfo=UTC)
    parsed = FeedDocumentParser().parse("agency", body, instant)
    assert len(parsed) == 1
    assert parsed[0].canonical_url == "https://agency.gov/release?id=1"
    assert "<b>" not in parsed[0].normalized_text
    assert len(deduplicate_documents([parsed[0], parsed[0]])) == 1


def test_canonical_url_drops_fragment_and_tracking() -> None:
    assert (
        canonicalize_url("HTTPS://Example.com/a/?utm_medium=x&item=2#part")
        == "https://example.com/a?item=2"
    )


def test_federal_score_is_transparent_and_confidence_adjusted() -> None:
    instant = datetime(2026, 9, 1, tzinfo=UTC)
    relationship = FederalRelationship(
        created_at=instant,
        ticker="TEST",
        issuer_name="Test Corp",
        agency="Department of Energy",
        relationship_type=RelationshipType.STRATEGIC_INVESTMENT,
        announcement_date=date(2026, 8, 1),
        primary_source_url="https://energy.gov/example",
        source_publication_date=date(2026, 8, 1),
        confidence=Decimal("0.90"),
        last_verified_at=instant,
    )
    score = FederalExposureScorer().score([relationship])
    assert score.value == Decimal("73.80")
    assert "Department of Energy" in score.explanation[0]
