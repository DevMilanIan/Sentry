from __future__ import annotations

from collections.abc import Mapping
from typing import TypeVar

from pydantic import BaseModel

from app.domain.models import sha256_json
from app.exceptions import DataInvalidError
from app.reasoning.provider import (
    LocalModelProvider,
    ModelCallMetrics,
    ModelCallResult,
    ModelHealth,
    ReasoningRole,
)

TOutput = TypeVar("TOutput", bound=BaseModel)


class ScriptedReplayModelProvider(LocalModelProvider):
    """Typed, tool-free model double for deterministic causal regression.

    It is intentionally explicit in audit records and cannot be mistaken for
    measured Ollama inference.  Offline acceptance uses it to prove controller
    determinism; separate integration/benchmark gates prove the real local
    model boundary.
    """

    def __init__(
        self,
        outputs: Mapping[ReasoningRole, BaseModel],
        *,
        script_version: str = "offline-e2e-reasoning-v1",
    ) -> None:
        if not outputs:
            raise ValueError("scripted replay outputs cannot be empty")
        self._outputs = dict(outputs)
        self._script_version = script_version
        self._model_digest = sha256_json(
            {
                "script_version": self._script_version,
                "outputs": {
                    role.value: output.model_dump(mode="json")
                    for role, output in sorted(
                        self._outputs.items(), key=lambda item: item[0].value
                    )
                },
            }
        )
        self.calls: list[ReasoningRole] = []

    @property
    def model_name(self) -> str:
        return self._script_version

    async def generate(
        self,
        *,
        role: ReasoningRole,
        prompt: str,
        response_model: type[TOutput],
        system_prompt: str = "",
        deep: bool = False,
    ) -> ModelCallResult[TOutput]:
        del prompt, system_prompt, deep
        scripted = self._outputs.get(role)
        if scripted is None:
            raise DataInvalidError(f"scripted replay has no output for role {role.value}")
        output = response_model.model_validate(scripted.model_dump(mode="python"))
        self.calls.append(role)
        content = output.model_dump(mode="json")
        return ModelCallResult[TOutput](
            output=output,
            role=role,
            model_name=self.model_name,
            model_digest=self._model_digest,
            provider="scripted-replay",
            metrics=ModelCallMetrics(latency_ms=0, repair_attempts=0),
            raw_response_hash=sha256_json(content),
        )

    async def health(self) -> ModelHealth:
        return ModelHealth(
            healthy=True,
            provider="scripted-replay",
            model_name=self.model_name,
            model_present=True,
            detail="versioned deterministic replay outputs loaded",
            model_digest=self._model_digest,
        )
