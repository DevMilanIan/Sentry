from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from pydantic import BaseModel

from app.domain.models import CandidatePacket, DomainModel
from app.exceptions import DataInvalidError

REFERENCE_FIELDS = frozenset(
    {
        "supporting_facts",
        "supporting_fact_ids",
        "referenced_fact_ids",
        "fact_ids",
    }
)


class GroundingResult(DomainModel):
    grounded: bool
    referenced_fact_ids: tuple[str, ...]
    unknown_fact_ids: tuple[str, ...]
    available_fact_ids: tuple[str, ...]
    inference_notes: tuple[str, ...] = ()
    errors: tuple[str, ...] = ()


class GroundingError(DataInvalidError):
    """A model cited evidence absent from its immutable candidate packet."""


def validate_grounding(
    output: BaseModel | dict[str, Any],
    packet: CandidatePacket,
    *,
    additional_fact_ids: Iterable[str] = (),
    require_reference: bool = True,
    raise_on_error: bool = True,
) -> GroundingResult:
    raw = output.model_dump(mode="python") if isinstance(output, BaseModel) else output
    references = tuple(sorted(set(_collect_references(raw))))
    available = tuple(sorted(set(packet.facts) | set(additional_fact_ids)))
    unknown = tuple(sorted(set(references) - set(available)))
    inference_notes = tuple(str(item) for item in raw.get("inference_notes", ()))
    errors: list[str] = []
    if unknown:
        errors.append(f"unknown fact references: {', '.join(unknown)}")
    if require_reference and not references and not inference_notes:
        errors.append("analysis contains neither packet references nor marked inferences")
    result = GroundingResult(
        grounded=not errors,
        referenced_fact_ids=references,
        unknown_fact_ids=unknown,
        available_fact_ids=available,
        inference_notes=inference_notes,
        errors=tuple(errors),
    )
    if errors and raise_on_error:
        raise GroundingError("; ".join(errors))
    return result


def _collect_references(value: Any, field_name: str | None = None) -> Iterable[str]:
    if isinstance(value, dict):
        for key, item in value.items():
            yield from _collect_references(item, str(key))
    elif isinstance(value, (tuple, list, set)):
        if field_name in REFERENCE_FIELDS:
            for item in value:
                if isinstance(item, str):
                    yield item
        else:
            for item in value:
                yield from _collect_references(item, field_name)
    elif field_name in REFERENCE_FIELDS and isinstance(value, str):
        yield value
