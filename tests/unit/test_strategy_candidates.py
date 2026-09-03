from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import UUID

import pytest

from app.clock.base import VirtualClock
from app.domain.enums import AttentionLevel
from app.exceptions import DataInvalidError
from app.strategy.candidates import CandidateFact, CandidatePacketBuilder, inspect_packet


def test_packet_builder_is_stable_compact_and_provenanced() -> None:
    now = datetime(2026, 1, 5, 15, 0, tzinfo=UTC)
    clock = VirtualClock(now)
    builder = CandidatePacketBuilder(clock)
    fact = CandidateFact(
        fact_id="price-change-1",
        value={"percent": "4.25"},
        source_id="quote:1",
        effective_at=now - timedelta(seconds=2),
        observed_at=now - timedelta(seconds=1),
    )
    values = dict(
        run_id=UUID("11111111-1111-4111-8111-111111111111"),
        symbol="test",
        attention=AttentionLevel.CANDIDATE,
        surveillance_score=Decimal("72.5"),
        facts=(fact,),
    )
    first = builder.build(**values)
    second = builder.build(**values)
    assert first.packet_id == second.packet_id
    assert first.content_hash == second.content_hash
    assert first.source_ids == ("quote:1",)
    assert inspect_packet(first).estimated_tokens < 4_000


def test_packet_builder_rejects_future_facts() -> None:
    now = datetime(2026, 1, 5, 15, 0, tzinfo=UTC)
    builder = CandidatePacketBuilder(VirtualClock(now))
    future = CandidateFact(
        fact_id="future",
        value="not visible",
        source_id="fixture",
        effective_at=now + timedelta(minutes=1),
        observed_at=now + timedelta(minutes=1),
    )
    with pytest.raises(DataInvalidError):
        builder.build(
            run_id=UUID("11111111-1111-4111-8111-111111111111"),
            symbol="TEST",
            attention=AttentionLevel.CANDIDATE,
            surveillance_score=50,
            facts=(future,),
        )
