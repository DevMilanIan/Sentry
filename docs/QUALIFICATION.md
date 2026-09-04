# Qualification record

## Status

Authenticated qualification has not started (0 of 5 regular sessions). Core credential-free
implementation and safety verification are in progress.
Robinhood authentication must not be requested until offline parity and baseline fault tests pass.
Funding must not be requested until five regular broker-shadow sessions pass.

## Credential-free evidence available — 2026-09-03

- Full deterministic entry/fill/profit-exit lifecycle and causal replay tests pass.
- Runtime snapshot restoration, accepted-order/journal crash gaps, and interrupted
  fill/position-journal repair have automated in-memory regression coverage.
- Pending order exposure, exact approvals, mode/kill-switch changes during review,
  replay exhaustion, duplicate intents, and unknown-submission protections are tested.
- The completed local-model benchmark selects 4B for routine latency; see
  `MODEL_BENCHMARK.md` and its machine-readable result.

These are engineering results, not authenticated sessions. Real PostgreSQL migrations,
commit failures, backup/restore, process/container/host restart, and WSL/Docker startup
remain unverified. The production broker-shadow composition still needs actual
authenticated schemas/account selection and read/review integration. No real brokerage
authorization, funding, or order transmission has occurred.

## Required evidence per session

- authenticated masked account fingerprint and capability snapshot;
- real observed account state, separate shadow snapshot, and anomaly checks;
- candidate packet/source/market/model/prompt/policy hashes;
- deterministic selector, hard-risk, exact approval, safe review, OrderIntent, and exact
  BrokerCommandIntent records;
- firewall denial and proof no write transport was called;
- reproducible shadow lifecycle/fill evidence and rejected-candidate outcomes;
- LIVE_CONSERVATIVE counterfactual and divergence reason;
- MCP latency/reconnect/schema-drift, health, restart, and incident summary.

Machine-readable reports will be written beneath the isolated `var/demo/broker_shadow/reports/`
directory and remain excluded from Git.
