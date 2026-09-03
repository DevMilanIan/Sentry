from __future__ import annotations

import json
from pathlib import Path

from pydantic import Field

from app.domain.enums import Direction, JudgeDecision
from app.domain.models import DomainModel
from app.reasoning.provider import ReasoningRole


class BenchmarkGroup(DomainModel):
    id_prefix: str
    count: int = Field(gt=0)
    task: str
    role: ReasoningRole
    fact_template: str
    counterfact_template: str
    expected_direction: Direction | None = None
    expected_decision: JudgeDecision | None = None
    must_flag_conflict: bool = False
    must_abstain: bool = False


class BenchmarkDefinition(DomainModel):
    version: str
    groups: tuple[BenchmarkGroup, ...]


class BenchmarkCase(DomainModel):
    case_id: str
    suite_version: str
    task: str
    role: ReasoningRole
    facts: dict[str, str]
    expected_direction: Direction | None = None
    expected_decision: JudgeDecision | None = None
    must_flag_conflict: bool = False
    must_abstain: bool = False


def load_benchmark_cases(
    path: Path = Path(__file__).parent / "fixtures" / "reasoning_suite.json",
) -> tuple[BenchmarkCase, ...]:
    definition = BenchmarkDefinition.model_validate(json.loads(path.read_text(encoding="utf-8")))
    cases: list[BenchmarkCase] = []
    for group in definition.groups:
        for index in range(1, group.count + 1):
            case_id = f"{group.id_prefix}-{index:02d}"
            facts = {
                f"{case_id}:primary": group.fact_template.format(index=index),
                f"{case_id}:counter": group.counterfact_template.format(index=index),
            }
            if group.role is ReasoningRole.JUDGE:
                facts[f"{case_id}:contract_rank_1"] = (
                    "Validated deterministic option candidate rank 1 is available; "
                    "its liquidity and premium are inside all configured minimums."
                )
            cases.append(
                BenchmarkCase(
                    case_id=case_id,
                    suite_version=definition.version,
                    task=group.task,
                    role=group.role,
                    facts=facts,
                    expected_direction=group.expected_direction,
                    expected_decision=group.expected_decision,
                    must_flag_conflict=group.must_flag_conflict,
                    must_abstain=group.must_abstain,
                )
            )
    if len(cases) != 100:
        raise ValueError(f"benchmark definition must expand to exactly 100 cases, got {len(cases)}")
    if len({case.case_id for case in cases}) != len(cases):
        raise ValueError("benchmark case IDs must be unique")
    return tuple(cases)
