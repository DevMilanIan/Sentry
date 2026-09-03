from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any
from uuid import UUID, uuid5

from pydantic import Field, field_validator, model_validator

from app.clock.base import Clock
from app.domain.enums import AttentionLevel
from app.domain.models import (
    CandidatePacket,
    DomainModel,
    canonical_json,
    sha256_json,
)
from app.exceptions import DataInvalidError

PACKET_NAMESPACE = UUID("897c4d68-5047-5fde-88df-fdb5723bf04a")


class CandidateFact(DomainModel):
    fact_id: str = Field(min_length=1, max_length=128)
    value: Any
    source_id: str = Field(min_length=1, max_length=256)
    effective_at: datetime
    observed_at: datetime
    inference: bool = False

    @field_validator("effective_at", "observed_at")
    @classmethod
    def timestamps_are_utc(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("fact timestamps must be timezone-aware")
        return value.astimezone(UTC)

    @model_validator(mode="after")
    def causally_ordered(self) -> CandidateFact:
        if self.observed_at < self.effective_at:
            raise ValueError("fact cannot be observed before it is effective")
        try:
            canonical_json(self.value)
        except TypeError as exc:
            raise ValueError("fact value is not canonically serializable") from exc
        return self


class PacketManifest(DomainModel):
    packet_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    canonical_bytes: int = Field(ge=0)
    estimated_tokens: int = Field(ge=0)
    fact_ids: tuple[str, ...]


class CandidatePacketBuilder:
    """Build compact, deterministic packets using only facts visible to the Clock."""

    def __init__(self, clock: Clock, *, maximum_estimated_tokens: int = 4_000) -> None:
        if maximum_estimated_tokens <= 0:
            raise ValueError("maximum_estimated_tokens must be positive")
        self.clock = clock
        self.maximum_estimated_tokens = maximum_estimated_tokens

    def build(
        self,
        *,
        run_id: UUID,
        symbol: str,
        attention: AttentionLevel,
        surveillance_score: Decimal,
        facts: Sequence[CandidateFact] | Mapping[str, Any],
        source_ids: Sequence[str] = (),
        market_snapshot_ids: Sequence[UUID] = (),
    ) -> CandidatePacket:
        now = _aware_utc(self.clock.now(), "Clock.now()")
        normalized_symbol = symbol.strip().upper()
        if not normalized_symbol:
            raise ValueError("symbol cannot be empty")
        fact_map, derived_sources, available_at = self._normalize_facts(facts, now)
        all_sources = tuple(sorted(set(source_ids) | derived_sources))
        snapshot_ids = tuple(sorted(set(market_snapshot_ids), key=str))
        identity_payload = {
            "run_id": str(run_id),
            "symbol": normalized_symbol,
            "attention": int(attention),
            "surveillance_score": str(surveillance_score),
            "facts": fact_map,
            "source_ids": all_sources,
            "market_snapshot_ids": tuple(str(value) for value in snapshot_ids),
            "available_at": available_at,
        }
        packet_id = uuid5(PACKET_NAMESPACE, sha256_json(identity_payload))
        packet = CandidatePacket(
            packet_id=packet_id,
            run_id=run_id,
            symbol=normalized_symbol,
            attention=attention,
            surveillance_score=surveillance_score,
            facts=fact_map,
            source_ids=all_sources,
            market_snapshot_ids=snapshot_ids,
            available_at=available_at,
            created_at=now,
        )
        manifest = inspect_packet(packet)
        if manifest.estimated_tokens > self.maximum_estimated_tokens:
            raise DataInvalidError(
                f"candidate packet estimate {manifest.estimated_tokens} exceeds "
                f"limit {self.maximum_estimated_tokens}"
            )
        return packet

    def _normalize_facts(
        self, facts: Sequence[CandidateFact] | Mapping[str, Any], now: datetime
    ) -> tuple[dict[str, Any], set[str], datetime]:
        if isinstance(facts, Mapping):
            fact_map = {str(key): value for key, value in sorted(facts.items())}
            if not all(fact_map):
                raise ValueError("fact IDs cannot be empty")
            try:
                canonical_json(fact_map)
            except TypeError as exc:
                raise ValueError("facts are not canonically serializable") from exc
            return fact_map, set(), now
        materialized = tuple(facts)
        ids = [fact.fact_id for fact in materialized]
        if len(ids) != len(set(ids)):
            raise ValueError("candidate fact IDs must be unique")
        for fact in materialized:
            if fact.effective_at > now or fact.observed_at > now:
                raise DataInvalidError(f"fact {fact.fact_id} is not available at the replay clock")
        ordered = sorted(materialized, key=lambda item: item.fact_id)
        fact_map = {
            fact.fact_id: {
                "value": fact.value,
                "source_id": fact.source_id,
                "effective_at": fact.effective_at.isoformat(),
                "observed_at": fact.observed_at.isoformat(),
                "inference": fact.inference,
            }
            for fact in ordered
        }
        sources = {fact.source_id for fact in ordered}
        available_at = max(
            (max(fact.effective_at, fact.observed_at) for fact in ordered),
            default=now,
        )
        return fact_map, sources, available_at


def inspect_packet(packet: CandidatePacket) -> PacketManifest:
    value = packet.model_dump(mode="json", exclude={"created_at"})
    encoded = canonical_json(value).encode("utf-8")
    # Conservative local estimate: ASCII JSON often averages 3-4 bytes/token.
    estimated_tokens = (len(encoded) + 2) // 3
    return PacketManifest(
        packet_hash=sha256_json(value),
        canonical_bytes=len(encoded),
        estimated_tokens=estimated_tokens,
        fact_ids=tuple(sorted(packet.facts)),
    )


def validate_packet_availability(packet: CandidatePacket, clock: Clock) -> None:
    now = _aware_utc(clock.now(), "Clock.now()")
    available_at = _aware_utc(packet.available_at, "packet.available_at")
    if available_at > now:
        raise DataInvalidError("candidate packet is not yet available to the injected clock")
    for fact_id, raw in packet.facts.items():
        if not isinstance(raw, Mapping):
            continue
        for key in ("effective_at", "observed_at"):
            timestamp = raw.get(key)
            if timestamp is None:
                continue
            if isinstance(timestamp, str):
                timestamp = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
            if isinstance(timestamp, datetime) and _aware_utc(timestamp, key) > now:
                raise DataInvalidError(f"packet fact {fact_id} contains future {key}")


def _aware_utc(value: datetime, name: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{name} must be timezone-aware")
    return value.astimezone(UTC)


def _json_default(value: Any) -> str:
    if isinstance(value, (datetime, date, UUID, Decimal)):
        return str(value)
    raise TypeError(f"unsupported JSON type {type(value)!r}")


def compact_packet_json(packet: CandidatePacket) -> str:
    """Serialize the exact curated packet sent to a model, without whitespace."""

    return json.dumps(
        packet.model_dump(mode="json"),
        sort_keys=True,
        separators=(",", ":"),
        default=_json_default,
    )
