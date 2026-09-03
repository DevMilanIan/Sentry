from __future__ import annotations

from decimal import Decimal

from app.domain.enums import AttentionLevel
from app.strategy.attention import AttentionMapper, AttentionStage
from app.strategy.scoring import SurveillanceScorer, TradeQualityScorer


def test_surveillance_score_is_weighted_and_explained() -> None:
    values = {
        "catalyst_priority": 100,
        "momentum_anomaly": 80,
        "technical_structure": 60,
        "market_sector_alignment": 40,
        "underlying_liquidity": 100,
        "federal_exposure": 20,
        "event_urgency": 50,
    }
    result = SurveillanceScorer().score(values)
    assert result.final_score == Decimal("70.00")
    assert len(result.components) == 7
    assert not result.missing_components


def test_trade_score_applies_named_penalties() -> None:
    values = {
        "catalyst_materiality": 80,
        "adversarial_evidence": 80,
        "contract_execution": 80,
        "timing_confirmation": 80,
        "payoff_plausibility": 80,
        "market_sector_alignment": 80,
    }
    result = TradeQualityScorer().score_with_flags(
        values,
        penalty_flags={"stale_data": True, "extreme_spread": True},
    )
    assert result.base_score == Decimal("80.00")
    assert result.penalty_total == 35
    assert result.final_score == Decimal("45.00")


def test_attention_mapping_separates_surveillance_from_post_reasoning() -> None:
    mapper = AttentionMapper()
    assert mapper.map(Decimal("90")) is AttentionLevel.CANDIDATE
    assert (
        mapper.map(Decimal("90"), stage=AttentionStage.POST_REASONING)
        is AttentionLevel.DEEP_RESEARCH
    )
    assert mapper.map(Decimal("1"), has_position=True) is AttentionLevel.POSITION
