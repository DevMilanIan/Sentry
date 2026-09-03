from __future__ import annotations

from abc import ABC, abstractmethod
from enum import StrEnum
from typing import Literal, TypeVar

from pydantic import BaseModel, Field, model_validator

from app.domain.models import DomainModel

TOutput = TypeVar("TOutput", bound=BaseModel)

# Pydantic accepts int/float timeout input; this alias keeps public intent obvious.
DecimalPositive = float


class ReasoningRole(StrEnum):
    SITUATION = "situation"
    BULL = "bull"
    BEAR = "bear"
    SKEPTIC = "skeptic"
    JUDGE = "judge"
    DEEP_RESEARCH = "deep_research"


class ModelProviderConfig(DomainModel):
    version: str = "model-v1"
    provider: Literal["ollama"] = "ollama"
    base_url: str = "http://localhost:11434"
    model: str = Field(min_length=1)
    normal_timeout_seconds: DecimalPositive = Field(default=60, gt=0)
    deep_timeout_seconds: DecimalPositive = Field(default=180, gt=0)
    max_repair_attempts: int = Field(default=1, ge=0, le=3)
    context_token_limit: int = Field(default=8192, ge=512)
    temperature: float = Field(default=0.1, ge=0, le=2)
    judge_temperature: float = Field(default=0, ge=0, le=1)
    # Intraday roles need bounded, schema-valid answers rather than an unbounded
    # hidden reasoning trace.  A later deep-research provider may expose a
    # separately versioned thinking policy; this production profile is
    # intentionally non-thinking and finite.
    thinking_enabled: Literal[False] = False
    max_output_tokens: int = Field(default=1_024, ge=64, le=8_192)
    deterministic_seed: int = Field(default=1_729, ge=0)
    cloud_fallback: Literal[False] = False

    @model_validator(mode="after")
    def ollama_endpoint_only(self) -> ModelProviderConfig:
        normalized = self.base_url.lower().rstrip("/")
        if not normalized.startswith(("http://", "https://")):
            raise ValueError("Ollama base_url must be HTTP(S)")
        return self


class ModelCallMetrics(DomainModel):
    latency_ms: int = Field(ge=0)
    prompt_tokens: int | None = Field(default=None, ge=0)
    output_tokens: int | None = Field(default=None, ge=0)
    repair_attempts: int = Field(default=0, ge=0)


class ModelCallResult[TOutput: BaseModel](DomainModel):
    output: TOutput
    role: ReasoningRole
    model_name: str
    model_digest: str | None = None
    provider: str = "ollama"
    metrics: ModelCallMetrics
    raw_response_hash: str


class ModelHealth(DomainModel):
    healthy: bool
    provider: str = "ollama"
    model_name: str
    model_present: bool
    detail: str
    model_digest: str | None = None


class LocalModelProvider(ABC):
    """One local model interface; it intentionally exposes no tool execution."""

    @property
    @abstractmethod
    def model_name(self) -> str:
        pass

    @abstractmethod
    async def generate(
        self,
        *,
        role: ReasoningRole,
        prompt: str,
        response_model: type[TOutput],
        system_prompt: str = "",
        deep: bool = False,
    ) -> ModelCallResult[TOutput]:
        pass

    @abstractmethod
    async def health(self) -> ModelHealth:
        pass

    async def close(self) -> None:
        return None
