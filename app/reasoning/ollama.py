from __future__ import annotations

import json
import time
from typing import Any, TypeVar

import httpx
from pydantic import BaseModel, ValidationError

from app.domain.models import sha256_json
from app.exceptions import DataInvalidError, TransientError
from app.reasoning.provider import (
    LocalModelProvider,
    ModelCallMetrics,
    ModelCallResult,
    ModelHealth,
    ModelProviderConfig,
    ReasoningRole,
)

TOutput = TypeVar("TOutput", bound=BaseModel)


class ModelOutputValidationError(DataInvalidError):
    """All bounded structured-output and repair attempts failed validation."""


class ModelContextLimitError(DataInvalidError):
    """A curated packet exceeds the configured local model context budget."""


class OllamaModelProvider(LocalModelProvider):
    """Ollama-only structured provider with one bounded validation repair loop."""

    def __init__(
        self,
        config: ModelProviderConfig,
        *,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        if config.provider != "ollama" or config.cloud_fallback:
            raise ValueError("OllamaModelProvider cannot configure a cloud fallback")
        self.config = config
        self._owns_client = client is None
        self._client = client or httpx.AsyncClient(base_url=config.base_url.rstrip("/"))
        self._model_digest: str | None = None

    @property
    def model_name(self) -> str:
        return self.config.model

    async def generate(
        self,
        *,
        role: ReasoningRole,
        prompt: str,
        response_model: type[TOutput],
        system_prompt: str = "",
        deep: bool = False,
    ) -> ModelCallResult[TOutput]:
        if self._model_digest is None and self._owns_client:
            # One cheap local lookup binds each persisted call to the exact
            # installed model artifact rather than only its mutable tag.
            await self.health()
        # Ollama converts JSON Schema to a llama.cpp grammar.  Pydantic's raw
        # Decimal schema contains regex look-aheads and local $refs that the
        # Windows runner cannot currently compile.  Preserve the structural
        # shape for constrained generation and let Pydantic enforce the richer
        # bounds after generation.
        schema = _ollama_compatible_schema(response_model.model_json_schema())
        estimated = (
            _token_estimate(system_prompt)
            + _token_estimate(prompt)
            + _token_estimate(json.dumps(schema, separators=(",", ":")))
        )
        if estimated > self.config.context_token_limit:
            raise ModelContextLimitError(
                f"estimated context {estimated} exceeds limit {self.config.context_token_limit}"
            )
        messages = []
        if system_prompt:
            messages.append({"role": "system", "content": system_prompt})
        messages.append({"role": "user", "content": prompt})
        timeout = self.config.deep_timeout_seconds if deep else self.config.normal_timeout_seconds
        temperature = (
            self.config.judge_temperature
            if role is ReasoningRole.JUDGE
            else self.config.temperature
        )
        started = time.perf_counter()
        total_prompt_tokens = 0
        total_output_tokens = 0
        last_error = "unknown structured-output failure"
        last_content = ""
        model_digest: str | None = None
        for attempt in range(self.config.max_repair_attempts + 1):
            request_messages = list(messages)
            if attempt:
                request_messages.append(
                    {
                        "role": "user",
                        "content": _repair_prompt(last_content, last_error),
                    }
                )
            payload = {
                "model": self.config.model,
                "stream": False,
                "format": schema,
                "messages": request_messages,
                "think": self.config.thinking_enabled,
                "options": {
                    "temperature": temperature,
                    "num_ctx": self.config.context_token_limit,
                    "num_predict": self.config.max_output_tokens,
                    "seed": self.config.deterministic_seed,
                },
                "keep_alive": "5m",
            }
            response_payload = await self._post_chat(payload, request_timeout=float(timeout))
            total_prompt_tokens += _optional_int(response_payload.get("prompt_eval_count")) or 0
            total_output_tokens += _optional_int(response_payload.get("eval_count")) or 0
            model_digest = _model_digest(response_payload) or model_digest or self._model_digest
            try:
                last_content = _response_content(response_payload)
                decoded = _decode_json_object(last_content)
                output = response_model.model_validate(decoded)
                latency_ms = int((time.perf_counter() - started) * 1_000)
                return ModelCallResult[TOutput](
                    output=output,
                    role=role,
                    model_name=self.config.model,
                    model_digest=model_digest,
                    metrics=ModelCallMetrics(
                        latency_ms=latency_ms,
                        prompt_tokens=total_prompt_tokens or None,
                        output_tokens=total_output_tokens or None,
                        repair_attempts=attempt,
                    ),
                    raw_response_hash=sha256_json({"content": last_content}),
                )
            except (json.JSONDecodeError, ValidationError, TypeError, ValueError) as exc:
                last_error = _bounded_error(exc)
        raise ModelOutputValidationError(
            f"{role.value} output invalid after {self.config.max_repair_attempts + 1} "
            f"attempt(s): {last_error}"
        )

    async def _post_chat(
        self, payload: dict[str, Any], *, request_timeout: float
    ) -> dict[str, Any]:
        try:
            response = await self._client.post("/api/chat", json=payload, timeout=request_timeout)
            response.raise_for_status()
            parsed = response.json()
            if not isinstance(parsed, dict):
                raise TypeError("Ollama response must be an object")
            return parsed
        except httpx.TimeoutException as exc:
            raise TransientError("Ollama request timed out") from exc
        except httpx.HTTPStatusError as exc:
            detail = " ".join(exc.response.text.split())[:500]
            suffix = f": {detail}" if detail else ""
            raise TransientError(
                f"Ollama HTTP failure: {exc.response.status_code}{suffix}"
            ) from exc
        except (httpx.RequestError, json.JSONDecodeError, TypeError) as exc:
            raise TransientError(f"invalid Ollama response: {exc}") from exc

    async def health(self) -> ModelHealth:
        try:
            response = await self._client.get(
                "/api/tags", timeout=float(self.config.normal_timeout_seconds)
            )
            response.raise_for_status()
            payload = response.json()
            models = payload.get("models", []) if isinstance(payload, dict) else []
            present = any(
                item.get("name") == self.config.model or item.get("model") == self.config.model
                for item in models
                if isinstance(item, dict)
            )
            matched = next(
                (
                    item
                    for item in models
                    if isinstance(item, dict)
                    and (
                        item.get("name") == self.config.model
                        or item.get("model") == self.config.model
                    )
                ),
                None,
            )
            digest = matched.get("digest") if isinstance(matched, dict) else None
            self._model_digest = digest if isinstance(digest, str) else None
            return ModelHealth(
                healthy=present,
                model_name=self.config.model,
                model_present=present,
                detail=(
                    "model available" if present else "Ollama reachable; configured model absent"
                ),
                model_digest=self._model_digest,
            )
        except (httpx.HTTPError, json.JSONDecodeError, TypeError, ValueError) as exc:
            return ModelHealth(
                healthy=False,
                model_name=self.config.model,
                model_present=False,
                detail=f"Ollama unavailable: {type(exc).__name__}",
                model_digest=None,
            )

    async def close(self) -> None:
        if self._owns_client:
            await self._client.aclose()


def _response_content(payload: dict[str, Any]) -> str:
    message = payload.get("message")
    if not isinstance(message, dict) or "content" not in message:
        raise TypeError("Ollama response lacks message.content")
    content = message["content"]
    if isinstance(content, dict):
        return json.dumps(content, separators=(",", ":"))
    if not isinstance(content, str):
        raise TypeError("Ollama message.content must be a string or object")
    return content.strip()


def _ollama_compatible_schema(schema: dict[str, Any]) -> dict[str, Any]:
    """Reduce Pydantic JSON Schema to Ollama's portable grammar subset.

    The returned schema still constrains object/array/scalar shapes, required
    fields, enums, and nullability.  Min/max/regex/length validation remains a
    second, authoritative Pydantic pass and participates in the bounded repair
    loop.
    """

    root = schema
    omitted = {
        "$defs",
        "title",
        "description",
        "default",
        "examples",
        "minimum",
        "maximum",
        "exclusiveMinimum",
        "exclusiveMaximum",
        "minLength",
        "maxLength",
        "minItems",
        "maxItems",
        "pattern",
    }

    def simplify(value: Any, trail: frozenset[str] = frozenset()) -> Any:
        if isinstance(value, list):
            return [simplify(item, trail) for item in value]
        if not isinstance(value, dict):
            return value
        reference = value.get("$ref")
        if isinstance(reference, str):
            if not reference.startswith("#/") or reference in trail:
                raise ModelOutputValidationError(
                    f"unsupported recursive or external JSON Schema reference: {reference}"
                )
            target: Any = root
            for component in reference[2:].split("/"):
                if not isinstance(target, dict) or component not in target:
                    raise ModelOutputValidationError(
                        f"unresolvable JSON Schema reference: {reference}"
                    )
                target = target[component]
            return simplify(target, trail | {reference})

        alternatives = value.get("anyOf")
        if isinstance(alternatives, list):
            simplified = [simplify(item, trail) for item in alternatives]
            numeric = [
                item
                for item in simplified
                if isinstance(item, dict) and item.get("type") == "number"
            ]
            patterned_decimal = [
                item
                for item in alternatives
                if isinstance(item, dict) and item.get("type") == "string" and "pattern" in item
            ]
            # Pydantic emits Decimal as number OR a regex-constrained string.
            # A JSON number is losslessly converted to Decimal by validation.
            if len(numeric) == 1 and patterned_decimal:
                return numeric[0]

        result: dict[str, Any] = {}
        for key, item in value.items():
            if key in omitted or key == "$ref":
                continue
            result[key] = simplify(item, trail)
        return result

    result = simplify(schema)
    if not isinstance(result, dict):
        raise ModelOutputValidationError("root response schema must be an object")
    return result


def _decode_json_object(content: str) -> dict[str, Any]:
    cleaned = content.strip()
    if cleaned.startswith("```"):
        lines = cleaned.splitlines()
        if lines and lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        cleaned = "\n".join(lines).strip()
    parsed = json.loads(cleaned)
    if not isinstance(parsed, dict):
        raise TypeError("structured model output must be a JSON object")
    return parsed


def _repair_prompt(previous: str, error: str) -> str:
    bounded_previous = previous[:8_000]
    return (
        "Your prior response failed local schema validation. Return only a corrected JSON object. "
        "Do not add facts, citations, markdown, or commentary. Validation error: "
        f"{error[:2_000]}\nPrior response:\n{bounded_previous}"
    )


def _bounded_error(error: Exception) -> str:
    return str(error).replace("\n", " ")[:2_000]


def _token_estimate(value: str) -> int:
    return (len(value.encode("utf-8")) + 2) // 3


def _optional_int(value: Any) -> int | None:
    return value if isinstance(value, int) and value >= 0 else None


def _model_digest(payload: dict[str, Any]) -> str | None:
    digest = payload.get("model_digest") or payload.get("digest")
    return digest if isinstance(digest, str) else None
