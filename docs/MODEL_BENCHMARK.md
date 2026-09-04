# Local model verification — 2026-09-03

The selected routine model is `qwen3.5:4b`, using the same local Ollama provider,
4,096-token context, structured-output schemas, and sequential role execution.
No cloud fallback is enabled. The 9B model remains installed for later re-evaluation.

Evidence: `benchmarks/results/qwen35_2026-09-03_verified.json`, completed at
2026-09-03 19:43:59 UTC. Both candidates completed 100 cases.

| Measurement | qwen3.5:9b | qwen3.5:4b |
|---|---:|---:|
| JSON schema validity | 100% | 100% |
| Allowed fact-ID grounding | 99% | 99% |
| Fixture calibration metric | 100% | 97.78% |
| Contradiction detection metric | 100% | 100% |
| Median latency | 7,652 ms | 5,911 ms |
| p95 latency | 18,580 ms | 9,107 ms |
| Mean latency | 9,941.72 ms | 5,999.47 ms |
| Peak whole-GPU memory | 6,973 MiB | 6,941 MiB |
| Peak sampled GPU temperature | 57 C | 71 C |

Selection first requires quality acceptance, p95 <= 15,000 ms, and peak GPU memory
<= 7,500 MiB. It then ranks eligible models by weighted quality and latency. Only
4B passed all gates in the verified run. The earlier retained run had a 12,581 ms
9B p95; that result does not erase the later latency failure under observed load.

Exact model digests, per-case outputs, model memory, and telemetry are in the JSON.
Whole-GPU peaks include other/residual allocations; they are not model-size claims.
Grounding checks permitted citation IDs, not semantic entailment. Cases are heavily
templated and do not prove trading skill, profitability, real-world calibration,
unattended uptime, or broker qualification. Repeat the suite after material model,
prompt, driver, context, or hardware changes.
