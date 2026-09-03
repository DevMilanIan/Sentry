from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID

import pytest

from app.clock.base import VirtualClock
from app.domain.enums import AttentionLevel, Direction
from app.reasoning.grounding import GroundingError, validate_grounding
from app.reasoning.schemas import SituationAnalysis
from app.strategy.candidates import CandidatePacketBuilder


def test_grounding_detects_hallucinated_packet_references() -> None:
    clock = VirtualClock(datetime(2026, 1, 5, 15, 0, tzinfo=UTC))
    packet = CandidatePacketBuilder(clock).build(
        run_id=UUID("11111111-1111-4111-8111-111111111111"),
        symbol="TEST",
        attention=AttentionLevel.CANDIDATE,
        surveillance_score=50,
        facts={"known": "verified fact"},
    )
    output = SituationAnalysis(
        materiality="0.6",
        directional_bias=Direction.BULLISH,
        time_horizon="week",
        primary_driver="event",
        supporting_facts=("invented",),
        uncertainties=(),
        thesis_invalidation_conditions=("withdrawal",),
        research_needed=(),
        abstain_reason=None,
    )
    with pytest.raises(GroundingError):
        validate_grounding(output, packet)
