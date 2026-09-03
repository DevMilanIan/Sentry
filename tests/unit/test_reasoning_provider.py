from __future__ import annotations

import json

import httpx
import pytest

from app.domain.enums import Direction
from app.reasoning.ollama import OllamaModelProvider, _ollama_compatible_schema
from app.reasoning.provider import ModelProviderConfig, ReasoningRole
from app.reasoning.schemas import SituationAnalysis


@pytest.mark.asyncio
async def test_ollama_provider_repairs_invalid_structured_output_once() -> None:
    responses = iter(
        [
            {"message": {"content": "not-json"}, "eval_count": 1},
            {
                "message": {
                    "content": json.dumps(
                        {
                            "materiality": "0.7",
                            "directional_bias": "bullish",
                            "time_horizon": "ten sessions",
                            "primary_driver": "verified event",
                            "supporting_facts": ["f1"],
                            "uncertainties": [],
                            "thesis_invalidation_conditions": ["event withdrawn"],
                            "research_needed": [],
                            "abstain_reason": None,
                            "inference_notes": [],
                        }
                    )
                },
                "prompt_eval_count": 20,
                "eval_count": 30,
            },
        ]
    )

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/chat"
        payload = json.loads(request.content)
        assert payload["think"] is False
        assert payload["options"]["num_ctx"] == 8192
        assert payload["options"]["num_predict"] == 1024
        assert payload["options"]["seed"] == 1729
        return httpx.Response(200, json=next(responses))

    client = httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        base_url="http://localhost:11434",
    )
    provider = OllamaModelProvider(
        ModelProviderConfig(model="fixture", max_repair_attempts=1),
        client=client,
    )
    result = await provider.generate(
        role=ReasoningRole.SITUATION,
        prompt="fixture packet",
        response_model=SituationAnalysis,
    )
    assert result.output.directional_bias is Direction.BULLISH
    assert result.metrics.repair_attempts == 1
    await client.aclose()


def test_model_config_forbids_cloud_fallback() -> None:
    with pytest.raises(ValueError):
        ModelProviderConfig.model_validate(
            {"provider": "ollama", "model": "fixture", "cloud_fallback": True}
        )


def test_ollama_schema_flattens_refs_and_decimal_regexes() -> None:
    schema = _ollama_compatible_schema(SituationAnalysis.model_json_schema())

    assert "$defs" not in schema
    assert schema["properties"]["materiality"] == {"type": "number"}
    assert schema["properties"]["directional_bias"] == {
        "enum": ["bullish", "bearish", "mixed", "none"],
        "type": "string",
    }
    assert "pattern" not in json.dumps(schema)
