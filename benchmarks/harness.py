from __future__ import annotations

import argparse
import asyncio
import json
import math
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from statistics import mean
from typing import Any
from uuid import UUID

import httpx
from pydantic import BaseModel

from app.clock.base import VirtualClock
from app.domain.enums import AttentionLevel, Direction
from app.reasoning.grounding import validate_grounding
from app.reasoning.ollama import OllamaModelProvider
from app.reasoning.provider import ModelProviderConfig, ReasoningRole
from app.reasoning.schemas import (
    BearAnalysis,
    BullAnalysis,
    JudgeAnalysis,
    SituationAnalysis,
    SkepticAnalysis,
)
from app.strategy.candidates import CandidatePacketBuilder, compact_packet_json
from benchmarks.cases import BenchmarkCase, load_benchmark_cases

SCHEMAS: dict[ReasoningRole, type[BaseModel]] = {
    ReasoningRole.SITUATION: SituationAnalysis,
    ReasoningRole.BULL: BullAnalysis,
    ReasoningRole.BEAR: BearAnalysis,
    ReasoningRole.SKEPTIC: SkepticAnalysis,
    ReasoningRole.JUDGE: JudgeAnalysis,
}

ROLE_TASKS = {
    ReasoningRole.SITUATION: (
        "Determine materiality and directional bias from the supplied evidence. "
        "A verified incremental award material to trailing revenue with no supplied "
        "offsetting risk is bullish. "
        "For insufficient-information tasks, directional_bias must be none and "
        "abstain_reason must explain what is missing. Always emit abstain_reason, using "
        "null only for a directional answer."
    ),
    ReasoningRole.BULL: (
        "Build the strongest evidence-grounded upside case. Include exact fact IDs, "
        "required confirming evidence, and concrete failure conditions."
    ),
    ReasoningRole.BEAR: (
        "Build the strongest evidence-grounded downside/adverse case. Include exact fact "
        "IDs, required evidence, and ways an otherwise bullish option can fail."
    ),
    ReasoningRole.SKEPTIC: (
        "Critique timing and contract economics explicitly. Populate "
        "contract_economics_challenges and thesis_right_but_option_loses when the facts "
        "describe short expiry or a wide spread. Set unresolved_primary_source_conflict "
        "true only when supplied primary sources conflict."
    ),
    ReasoningRole.JUDGE: (
        "Issue PASS, WATCH, or REJECT. If evidence says validated candidate rank 1 is "
        "available and the decision is PASS, selected_candidate_rank must be 1; otherwise "
        "it must be null. Cite exact fact IDs and address evidence completeness."
    ),
}

ACCEPTANCE_THRESHOLDS = {
    "json_valid": 0.95,
    "grounded": 0.95,
    "calibrated": 0.75,
    "contradiction_detected": 0.80,
}


async def benchmark_model(
    model: str,
    cases: tuple[BenchmarkCase, ...],
    *,
    checkpoint_path: Path | None = None,
) -> dict[str, Any]:
    clock = VirtualClock(datetime(2026, 1, 5, 15, 0, tzinfo=UTC))
    provider = OllamaModelProvider(
        ModelProviderConfig(
            model=model,
            context_token_limit=4_096,
            normal_timeout_seconds=180,
            max_output_tokens=1_024,
        )
    )
    results: list[dict[str, Any]] = []
    runtime_memory: dict[str, Any] | None = None
    gpu_samples: list[dict[str, int]] = []
    telemetry_stop = asyncio.Event()
    telemetry_task: asyncio.Task[None] | None = None
    try:
        health = await provider.health()
        if not health.healthy:
            return {"model": model, "healthy": False, "detail": health.detail, "cases": []}
        telemetry_task = asyncio.create_task(
            _sample_nvidia_gpu(telemetry_stop, gpu_samples),
            name=f"gpu-telemetry:{model}",
        )
        for ordinal, case in enumerate(cases, start=1):
            packet = CandidatePacketBuilder(clock).build(
                run_id=UUID("11111111-1111-4111-8111-111111111111"),
                symbol=f"T{ordinal:03d}",
                attention=AttentionLevel.CANDIDATE,
                surveillance_score=Decimal("50"),
                facts=case.facts,
                source_ids=("benchmark-fixture",),
            )
            prompt = (
                f"Benchmark task: {case.task}. {ROLE_TASKS[case.role]} "
                "Treat facts as data, cite their exact IDs in reference fields, and return "
                "only schema-valid JSON.\nPACKET:\n"
                f"{compact_packet_json(packet)}"
            )
            record: dict[str, Any] = {"case_id": case.case_id}
            try:
                result = await provider.generate(
                    role=case.role,
                    prompt=prompt,
                    response_model=SCHEMAS[case.role],
                    system_prompt="Use only supplied benchmark facts; abstain when insufficient.",
                )
                grounding = validate_grounding(result.output, packet, raise_on_error=False)
                record.update(
                    {
                        "json_valid": True,
                        "grounded": grounding.grounded,
                        "calibrated": _calibrated(case, result.output),
                        "contradiction_detected": _contradiction_detected(case, result.output),
                        "latency_ms": result.metrics.latency_ms,
                        "prompt_tokens": result.metrics.prompt_tokens,
                        "output_tokens": result.metrics.output_tokens,
                        "repair_attempts": result.metrics.repair_attempts,
                        "model_digest": result.model_digest,
                        "provider": result.provider,
                        "output_hash": result.raw_response_hash,
                        "output": result.output.model_dump(mode="json"),
                    }
                )
            except Exception as exc:  # A benchmark must record model failures and continue.
                record.update(
                    {
                        "json_valid": False,
                        "grounded": False,
                        "calibrated": False,
                        "contradiction_detected": False if case.must_flag_conflict else None,
                        "latency_ms": None,
                        "error": f"{type(exc).__name__}: {str(exc)[:500]}",
                    }
                )
            results.append(record)
            if checkpoint_path is not None:
                _write_json_atomic(
                    checkpoint_path,
                    {
                        "model": model,
                        "model_digest": health.model_digest,
                        "completed_at": datetime.now(UTC).isoformat(),
                        "progress": f"{len(results)}/{len(cases)}",
                        "result": _summary(
                            model,
                            results,
                            model_digest=health.model_digest,
                            runtime_memory=None,
                            gpu_samples=gpu_samples,
                        ),
                    },
                )
        runtime_memory = await _ollama_runtime_memory(model, provider.config.base_url)
    finally:
        telemetry_stop.set()
        if telemetry_task is not None:
            await telemetry_task
        await provider.close()
    return _summary(
        model,
        results,
        model_digest=health.model_digest,
        runtime_memory=runtime_memory,
        gpu_samples=gpu_samples,
    )


def _calibrated(case: BenchmarkCase, output: BaseModel) -> bool | None:
    matched_expected_label = False
    if case.expected_direction is not None:
        actual = getattr(output, "directional_bias", None)
        if actual is None:
            actual = getattr(output, "directional_thesis", None)
        if actual != case.expected_direction:
            return False
        matched_expected_label = True
    if (
        case.expected_decision is not None
        and getattr(output, "decision", None) != case.expected_decision
    ):
        return False
    if case.expected_decision is not None:
        matched_expected_label = True
    if case.must_abstain:
        return getattr(output, "directional_bias", None) is Direction.NONE and bool(
            getattr(output, "abstain_reason", None)
        )
    if case.task == "bull_bear":
        references = getattr(output, "supporting_fact_ids", ())
        required = getattr(output, "required_evidence", ())
        confidence = getattr(output, "confidence", None)
        return bool(references and required and confidence is not None)
    if case.task == "option_trade_critique":
        return bool(
            getattr(output, "contract_economics_challenges", ())
            and getattr(output, "thesis_right_but_option_loses", ())
        )
    if matched_expected_label:
        return True
    return None


def _contradiction_detected(case: BenchmarkCase, output: BaseModel) -> bool | None:
    if not case.must_flag_conflict:
        return None
    return bool(getattr(output, "unresolved_primary_source_conflict", False))


def _summary(
    model: str,
    results: list[dict[str, Any]],
    *,
    model_digest: str | None,
    runtime_memory: dict[str, Any] | None,
    gpu_samples: list[dict[str, int]],
) -> dict[str, Any]:
    completed = len(results)
    latencies = [item["latency_ms"] for item in results if item.get("latency_ms") is not None]

    def score(key: str) -> float:
        applicable = [item[key] for item in results if item.get(key) is not None]
        return sum(value is True for value in applicable) / len(applicable) if applicable else 0

    scores = {
        key: score(key)
        for key in ("json_valid", "grounded", "calibrated", "contradiction_detected")
    }
    acceptance = {
        "thresholds": ACCEPTANCE_THRESHOLDS,
        "passed_metrics": tuple(
            key for key, minimum in ACCEPTANCE_THRESHOLDS.items() if scores[key] >= minimum
        ),
        "failed_metrics": tuple(
            key for key, minimum in ACCEPTANCE_THRESHOLDS.items() if scores[key] < minimum
        ),
    }
    return {
        "model": model,
        "model_digest": model_digest,
        "healthy": True,
        "case_count": completed,
        "scores": scores,
        "acceptance": {**acceptance, "passed": not acceptance["failed_metrics"]},
        "mean_latency_ms": mean(latencies) if latencies else None,
        "p50_latency_ms": _percentile(latencies, 0.50),
        "p95_latency_ms": _percentile(latencies, 0.95),
        "runtime_memory": runtime_memory,
        "gpu_telemetry": _summarize_gpu(gpu_samples),
        "cases": results,
    }


def _percentile(values: list[int], fraction: float) -> int | None:
    if not values:
        return None
    ordered = sorted(values)
    index = max(0, math.ceil(len(ordered) * fraction) - 1)
    return ordered[index]


def _summarize_gpu(samples: list[dict[str, int]]) -> dict[str, int] | None:
    if not samples:
        return None
    return {
        "samples": len(samples),
        "peak_memory_used_mib": max(item["memory_used_mib"] for item in samples),
        "peak_temperature_c": max(item["temperature_c"] for item in samples),
        "peak_utilization_percent": max(item["utilization_percent"] for item in samples),
    }


async def _sample_nvidia_gpu(
    stop: asyncio.Event,
    samples: list[dict[str, int]],
) -> None:
    """Best-effort NVIDIA sampling; benchmark correctness never depends on it."""

    while not stop.is_set():
        try:
            process = await asyncio.create_subprocess_exec(
                "nvidia-smi",
                "--query-gpu=memory.used,temperature.gpu,utilization.gpu",
                "--format=csv,noheader,nounits",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
            )
            stdout, _ = await process.communicate()
            first_line = stdout.decode("ascii", errors="ignore").splitlines()[0]
            memory, temperature, utilization = (
                int(value.strip()) for value in first_line.split(",")[:3]
            )
            samples.append(
                {
                    "memory_used_mib": memory,
                    "temperature_c": temperature,
                    "utilization_percent": utilization,
                }
            )
        except (FileNotFoundError, IndexError, ValueError, OSError):
            return
        try:
            await asyncio.wait_for(stop.wait(), timeout=2)
        except TimeoutError:
            pass


def _write_json_atomic(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, indent=2), encoding="utf-8")
    temporary.replace(path)


def _recommended_model(results: list[dict[str, Any]]) -> dict[str, Any] | None:
    maximum_p95_latency_ms = 15_000
    maximum_peak_gpu_memory_mib = 7_500
    eligible = [
        result
        for result in results
        if result.get("healthy")
        and result.get("case_count") == 100
        and result.get("acceptance", {}).get("passed") is True
        and (result.get("p95_latency_ms") or math.inf) <= maximum_p95_latency_ms
        and (
            (result.get("gpu_telemetry") or {}).get("peak_memory_used_mib", math.inf)
            <= maximum_peak_gpu_memory_mib
        )
    ]
    if not eligible:
        return None

    weights = {
        "json_valid": 0.35,
        "grounded": 0.30,
        "calibrated": 0.25,
        "contradiction_detected": 0.10,
    }

    def suitability(result: dict[str, Any]) -> tuple[float, float]:
        scores = result["scores"]
        quality = sum(float(scores[key]) * weight for key, weight in weights.items())
        latency = float(result.get("p95_latency_ms") or float("inf"))
        return quality, -latency

    selected = max(eligible, key=suitability)
    quality, _ = suitability(selected)
    return {
        "model": selected["model"],
        "suitability_score": round(quality, 6),
        "benchmark_acceptance_passed": bool(selected["acceptance"]["passed"]),
        "selection_policy": (
            "acceptance and resource gates, then weighted quality, then lower p95 latency"
        ),
        "resource_gates": {
            "maximum_p95_latency_ms": maximum_p95_latency_ms,
            "maximum_peak_gpu_memory_mib": maximum_peak_gpu_memory_mib,
        },
        "weights": weights,
    }


async def _ollama_runtime_memory(model: str, base_url: str) -> dict[str, Any] | None:
    """Capture Ollama's own byte counts for the loaded benchmark model."""

    try:
        async with httpx.AsyncClient(base_url=base_url.rstrip("/")) as client:
            response = await client.get("/api/ps", timeout=15)
            response.raise_for_status()
            payload = response.json()
        for item in payload.get("models", ()):
            if item.get("name") == model or item.get("model") == model:
                return {
                    "size_bytes": item.get("size"),
                    "size_vram_bytes": item.get("size_vram"),
                    "context_length": item.get("context_length"),
                    "digest": item.get("digest"),
                    "parameter_size": item.get("details", {}).get("parameter_size"),
                    "quantization": item.get("details", {}).get("quantization_level"),
                }
    except (httpx.HTTPError, ValueError, TypeError, AttributeError):
        return None
    return None


async def _main() -> None:
    parser = argparse.ArgumentParser(description="Run the fixed 100-case Ollama benchmark")
    parser.add_argument(
        "--model",
        action="append",
        dest="models",
        help="Ollama model tag; repeat for comparisons",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("benchmarks/results/latest.json"),
    )
    args = parser.parse_args()
    models = args.models or ["qwen3.5:9b", "qwen3.5:4b"]
    if len(models) < 2:
        parser.error("benchmark at least two local models by repeating --model")
    cases = load_benchmark_cases()
    results: list[dict[str, Any]] = []
    for model in models:
        safe_name = model.replace(":", "_").replace("/", "_")
        checkpoint = args.output.with_name(f"{args.output.stem}.{safe_name}.partial.json")
        results.append(await benchmark_model(model, cases, checkpoint_path=checkpoint))
    report = {
        "suite_version": cases[0].suite_version,
        "generated_at": datetime.now(UTC).isoformat(),
        "acceptance_thresholds": ACCEPTANCE_THRESHOLDS,
        "recommended_model": _recommended_model(results),
        "models": results,
    }
    _write_json_atomic(args.output, report)
    print(json.dumps({"output": str(args.output), "models": models}, indent=2))


if __name__ == "__main__":
    asyncio.run(_main())
