from __future__ import annotations

import unicodedata
from enum import StrEnum
from typing import Literal

from pydantic import Field, model_validator

from app.catalysts.models import SourceDocument
from app.config import IssuerAliasMappingConfig, SourcesConfig
from app.domain.models import DomainModel, sha256_json


class EntityMappingStatus(StrEnum):
    NO_MATCH = "NO_MATCH"
    MAPPED = "MAPPED"
    AMBIGUOUS = "AMBIGUOUS"


class IssuerAliasMatch(DomainModel):
    mapping_id: str
    ticker: str
    issuer_name: str
    matched_aliases: tuple[str, ...] = Field(min_length=1, max_length=20)
    provenance_url: str


class SourceEntityMapping(DomainModel):
    """Exact classification evidence stored with the resulting sentinel event."""

    status: EntityMappingStatus
    document_id: str
    source_id: str
    document_content_hash: str = Field(pattern=r"^[a-f0-9]{64}$")
    mapping_config_version: str
    mapping_config_digest: str = Field(pattern=r"^[a-f0-9]{64}$")
    mapped_ticker: str | None = None
    matches: tuple[IssuerAliasMatch, ...] = ()
    operator_maintained: Literal[True] = True

    @model_validator(mode="after")
    def result_is_consistent(self) -> SourceEntityMapping:
        tickers = {match.ticker for match in self.matches}
        if self.status is EntityMappingStatus.NO_MATCH and (
            self.matches or self.mapped_ticker is not None
        ):
            raise ValueError("NO_MATCH cannot contain issuer matches")
        if self.status is EntityMappingStatus.MAPPED and (
            len(tickers) != 1 or self.mapped_ticker not in tickers
        ):
            raise ValueError("MAPPED requires exactly one matched ticker")
        if self.status is EntityMappingStatus.AMBIGUOUS and (
            len(tickers) < 2 or self.mapped_ticker is not None
        ):
            raise ValueError("AMBIGUOUS requires multiple tickers and no selected ticker")
        if self.operator_maintained is not True:
            raise ValueError("issuer classifications must be explicitly operator maintained")
        return self


def _tokens(value: str) -> tuple[str, ...]:
    normalized = unicodedata.normalize("NFKC", value).casefold()
    flattened = "".join(character if character.isalnum() else " " for character in normalized)
    return tuple(flattened.split())


def _contains_tokens(haystack: tuple[str, ...], needle: tuple[str, ...]) -> bool:
    if not needle or len(needle) > len(haystack):
        return False
    return any(
        haystack[index : index + len(needle)] == needle
        for index in range(len(haystack) - len(needle) + 1)
    )


class ExplicitIssuerMapper:
    """Match only configured literal entity aliases, scoped to one official source."""

    def __init__(self, config: SourcesConfig) -> None:
        self.config_version = config.version
        self._mappings = tuple(sorted(config.issuer_mappings, key=lambda item: item.mapping_id))
        self.config_digest = sha256_json(
            {
                "version": config.version,
                "official_sources": [
                    source.model_dump(mode="json")
                    for source in sorted(config.official_sources, key=lambda item: item.id)
                ],
                "issuer_mappings": [mapping.model_dump(mode="json") for mapping in self._mappings],
            }
        )

    def map(self, document: SourceDocument) -> SourceEntityMapping:
        title_tokens = _tokens(document.title)
        body_tokens = _tokens(document.normalized_text)
        matches: list[IssuerAliasMatch] = []
        for mapping in self._mappings:
            if document.source_id not in mapping.source_ids:
                continue
            matched = tuple(
                sorted(
                    alias
                    for alias in mapping.aliases
                    if _contains_tokens(title_tokens, _tokens(alias))
                    or _contains_tokens(body_tokens, _tokens(alias))
                )
            )
            if matched:
                matches.append(self._match(mapping, matched))
        ordered = tuple(sorted(matches, key=lambda item: item.mapping_id))
        tickers = {match.ticker for match in ordered}
        if not tickers:
            status, ticker = EntityMappingStatus.NO_MATCH, None
        elif len(tickers) == 1:
            status, ticker = EntityMappingStatus.MAPPED, next(iter(tickers))
        else:
            # A multi-issuer release is retained for review but cannot wake a
            # single-symbol candidate from an arbitrary choice.
            status, ticker = EntityMappingStatus.AMBIGUOUS, None
        return SourceEntityMapping(
            status=status,
            document_id=str(document.document_id),
            source_id=document.source_id,
            document_content_hash=document.content_hash,
            mapping_config_version=self.config_version,
            mapping_config_digest=self.config_digest,
            mapped_ticker=ticker,
            matches=ordered,
        )

    @staticmethod
    def _match(
        mapping: IssuerAliasMappingConfig, matched_aliases: tuple[str, ...]
    ) -> IssuerAliasMatch:
        return IssuerAliasMatch(
            mapping_id=mapping.mapping_id,
            ticker=mapping.ticker,
            issuer_name=mapping.issuer_name,
            matched_aliases=matched_aliases,
            provenance_url=mapping.provenance_url,
        )
