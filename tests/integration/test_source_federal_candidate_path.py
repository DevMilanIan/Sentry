from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import httpx

from app.catalysts.collector import OfficialSourceCollector
from app.catalysts.runtime import CatalystIngestionWorker
from app.clock.base import VirtualClock
from app.config import (
    IssuerAliasMappingConfig,
    OfficialSourceConfig,
    SourcesConfig,
    load_config,
)
from app.db.repository import InMemoryAuditRepository
from app.domain.enums import DemoBackend, Direction, ExecutionEnvironment, OptionType
from app.domain.models import (
    EquityQuote,
    OptionContract,
    OptionQuote,
    ProviderMetadata,
    SentinelEvent,
)
from app.federal.registry import RelationshipType
from app.federal.service import FederalRegistryService, RelationshipDraft
from app.market.base import MarketDataProvider
from app.market.models import EquityScanRequest, MarketDataCapabilities, PriceBar
from app.reasoning.provider import ReasoningRole
from app.reasoning.schemas import SituationAnalysis
from app.reasoning.scripted import ScriptedReplayModelProvider
from app.strategy.runtime import CandidateResearchWorker


class CurrentFixtureMarket(MarketDataProvider):
    def __init__(self, observed_at: datetime) -> None:
        metadata = ProviderMetadata(
            provider=self.identity,
            capability_version=self.capability_version,
            observed_at=observed_at,
            effective_at=observed_at,
        )
        self.equity = EquityQuote(
            symbol="ACME",
            bid=Decimal("99.99"),
            ask=Decimal("100.01"),
            last=Decimal("100"),
            volume=200_000,
            metadata=metadata,
        )
        self.option = OptionQuote(
            contract=OptionContract(
                instrument_id="ACME-20260918-C-102",
                symbol="ACME",
                option_type=OptionType.CALL,
                strike=Decimal("102"),
                expiration=datetime(2026, 9, 18, tzinfo=UTC).date(),
            ),
            bid=Decimal("0.08"),
            ask=Decimal("0.10"),
            last=Decimal("0.09"),
            mark=Decimal("0.09"),
            volume=100,
            open_interest=500,
            implied_volatility=Decimal("0.4"),
            delta=Decimal("0.4"),
            metadata=metadata,
        )

    @property
    def identity(self) -> str:
        return "current-fixture-market"

    @property
    def capability_version(self) -> str:
        return "current-fixture-v1"

    @property
    def capabilities(self) -> MarketDataCapabilities:
        return MarketDataCapabilities(replay=False)

    async def get_equity_quote(self, symbol: str, *, as_of: datetime | None = None) -> EquityQuote:
        assert symbol == "ACME" and as_of is not None
        return self.equity

    async def get_bars(
        self,
        symbol: str,
        *,
        start: datetime | None = None,
        end: datetime | None = None,
        timeframe: str | None = None,
        as_of: datetime | None = None,
    ) -> Sequence[PriceBar]:
        return ()

    async def get_option_chain(
        self, symbol: str, *, as_of: datetime | None = None
    ) -> Sequence[OptionQuote]:
        assert symbol == "ACME" and as_of is not None
        return (self.option,)

    async def get_option_quote(
        self, instrument_id: str, *, as_of: datetime | None = None
    ) -> OptionQuote:
        assert instrument_id == self.option.contract.instrument_id
        return self.option

    async def scan_equities(
        self, request: EquityScanRequest, *, as_of: datetime | None = None
    ) -> Sequence[EquityQuote]:
        return (self.equity,)


def relationship(
    instant: datetime,
    *,
    kind: RelationshipType,
    verified: bool = True,
) -> RelationshipDraft:
    return RelationshipDraft(
        ticker="ACME",
        issuer_name="Acme Industries",
        agency="Synthetic Test Agency",
        relationship_type=kind,
        announcement_date=instant.date(),
        source_publication_date=instant.date(),
        primary_source_url="https://www.energy.gov/synthetic-test-not-a-real-record",
        source_available_at=instant,
        last_verified_at=instant if verified else None,
        confidence=Decimal("0.9") if kind is RelationshipType.STRATEGIC_INVESTMENT else 1,
    )


async def test_official_mapping_and_causal_federal_score_reach_one_candidate_packet() -> None:
    instant = datetime(2026, 9, 1, 14, 0, tzinfo=UTC)
    clock = VirtualClock(instant)
    sources = SourcesConfig(
        version="source-candidate-path-v1",
        sec_user_agent="OptionsSentinel integration fixture",
        poll_seconds=900,
        official_sources=(
            OfficialSourceConfig(
                id="energy",
                url="https://www.energy.gov/feed",
                enabled=True,
                surveillance_priority=100,
            ),
        ),
        issuer_mappings=(
            IssuerAliasMappingConfig(
                mapping_id="acme-industries",
                ticker="ACME",
                issuer_name="Acme Industries",
                aliases=("Acme Industries",),
                source_ids=("energy",),
                provenance_url="https://www.energy.gov/synthetic-issuer-map",
            ),
        ),
    )
    loaded = load_config()
    thresholds = loaded.strategy.attention_thresholds.model_copy(
        update={"l1_watch": 20, "l2_candidate": 35}
    )
    loaded = loaded.model_copy(
        update={
            "app": loaded.app.model_copy(
                update={
                    "execution_environment": ExecutionEnvironment.DEMO,
                    "demo_backend": DemoBackend.BROKER_SHADOW,
                }
            ),
            "sources": sources,
            "strategy": loaded.strategy.model_copy(update={"attention_thresholds": thresholds}),
        }
    )
    repository = InMemoryAuditRepository(loaded.bind_runtime())
    registry = FederalRegistryService(repository, clock)
    verified = await registry.create(
        relationship(instant, kind=RelationshipType.STRATEGIC_INVESTMENT),
        actor="test-operator",
        reason="verified synthetic path fixture",
    )
    unverified = await registry.create(
        relationship(instant, kind=RelationshipType.COMMON_EQUITY, verified=False),
        actor="test-operator",
        reason="unverified synthetic path fixture",
    )

    feed = b"""<rss><channel><item><title>Program notice for Acme Industries</title>
<link>https://www.energy.gov/synthetic-release</link>
<description>Acme Industries is named in this synthetic integration fixture.</description>
<pubDate>Tue, 01 Sep 2026 13:55:00 GMT</pubDate></item></channel></rss>"""

    def respond(request: httpx.Request) -> httpx.Response:
        assert str(request.url) == "https://www.energy.gov/feed"
        return httpx.Response(200, content=feed)

    collector = OfficialSourceCollector(
        clock,
        user_agent=sources.sec_user_agent,
        transport=httpx.MockTransport(respond),
    )
    await CatalystIngestionWorker(sources, clock, repository, collector).poll()
    document_row = (await repository.list("source_documents"))[0]
    assert document_row["payload"]["tickers"] == []  # Raw feed did not assert a ticker.
    event = SentinelEvent.model_validate((await repository.list("sentinel_events"))[0]["payload"])
    mapping = event.payload["entity_mapping"]
    assert event.tickers == ("ACME",)
    assert mapping["status"] == "MAPPED" and mapping["mapped_ticker"] == "ACME"
    assert mapping["matches"][0]["mapping_id"] == "acme-industries"

    # A stronger relationship recorded after event availability must not leak backward.
    await clock.advance(timedelta(seconds=1))
    future = await registry.create(
        relationship(clock.now(), kind=RelationshipType.COMMON_EQUITY),
        actor="test-operator",
        reason="post-event synthetic fixture",
    )
    provider = ScriptedReplayModelProvider(
        {
            ReasoningRole.SITUATION: SituationAnalysis(
                materiality=Decimal("0"),
                directional_bias=Direction.NONE,
                time_horizon="not applicable",
                primary_driver="integration path intentionally abstains",
                supporting_facts=("federal:score:ACME",),
                uncertainties=(),
                thesis_invalidation_conditions=(),
                research_needed=(),
                abstain_reason="this fixture verifies evidence flow, not a trade",
            )
        },
        script_version="source-federal-candidate-path-v1",
    )
    worker = CandidateResearchWorker(
        loaded,
        clock,
        repository,
        provider,
        policy_profile=loaded.decision_policies.profiles["DEMO_EXPLORATORY"],
        federal_registry=registry,
    )
    assert await worker.on_event(event, CurrentFixtureMarket(instant)) is None

    feature_rows = await repository.list("candidate_features")
    scored = next(row["payload"] for row in feature_rows if "surveillance" in row["payload"])
    federal_component = next(
        component
        for component in scored["surveillance"]["components"]
        if component["name"] == "federal_exposure"
    )
    assert Decimal(federal_component["raw_value"]) == Decimal("73.80")
    assert federal_component["supplied"] is True
    packets = await repository.list("candidate_packets")
    assert len(packets) == 1
    facts = packets[0]["payload"]["facts"]
    aggregate = facts["federal:score:ACME"]["value"]
    assert aggregate["included_relationship_ids"] == [str(verified.relationship_id)]
    assert aggregate["excluded_relationship_ids"] == [str(unverified.relationship_id)]
    assert str(future.relationship_id) not in str(aggregate)
    assert (
        f"federal:relationship:{unverified.relationship_id}:{unverified.revision_id}" not in facts
    )
    assert (
        facts[f"source:{document_row['payload']['document_id']}"]["value"]["entity_mapping"][
            "mapping_config_digest"
        ]
        == mapping["mapping_config_digest"]
    )
    assert provider.calls == [ReasoningRole.SITUATION]
    assert await repository.list("trade_proposals") == []

    restarted = CandidateResearchWorker(
        loaded,
        clock,
        repository,
        ScriptedReplayModelProvider(
            {
                ReasoningRole.SITUATION: SituationAnalysis(
                    materiality=0,
                    directional_bias=Direction.NONE,
                    time_horizon="not applicable",
                    primary_driver="must not be called",
                    supporting_facts=(),
                    uncertainties=(),
                    thesis_invalidation_conditions=(),
                    research_needed=(),
                    abstain_reason="must not be called",
                )
            }
        ),
        policy_profile=loaded.decision_policies.profiles["DEMO_EXPLORATORY"],
        federal_registry=registry,
    )
    assert await restarted.on_event(event, CurrentFixtureMarket(instant)) is None
    assert len(await repository.list("candidate_packets")) == 1
