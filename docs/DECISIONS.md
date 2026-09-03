# Architectural decisions

## ADR-001 — Modular monolith for V1

**Date:** 2026-09-02  
**Decision:** One typed Python application with internal asynchronous workers, FastAPI, and
PostgreSQL.  
**Alternatives:** Microservices; an LLM-driven orchestration loop.  
**Reason:** A modular monolith minimizes distributed failure modes while preserving hard module
boundaries. Deterministic Python owns state and scheduling.

## ADR-002 — Startup-bound execution environment

**Date:** 2026-09-02  
**Decision:** Bind `DEMO`/`LIVE`, DEMO backend, persistence schema, runtime directory, and
idempotency namespace once during startup. No API can change them.  
**Alternatives:** Hot-switched environment flag.  
**Reason:** Process restart plus reconciliation provides an auditable safety boundary and prevents
Demo state from becoming Live state.

## ADR-003 — Same command object before blocked or real transport

**Date:** 2026-09-02  
**Decision:** Persist a schema-valid `BrokerCommandIntent` before both shadow denial and any future
Live transport.  
**Alternatives:** Human-readable “would trade” logs.  
**Reason:** Exact intent, evidence, and schema provenance are required for broker-shadow parity and
safe idempotency.

## ADR-004 — No implicit external market-data dependency in offline qualification

**Date:** 2026-09-02  
**Decision:** Bundle timestamped replay fixtures; external feeds and Robinhood reads are injected
providers.  
**Alternatives:** Couple the simulator to one network feed.  
**Reason:** Reproducibility, zero-credential operation, and causal replay are mandatory.

## ADR-005 — Default-deny all MCP mutations

**Date:** 2026-09-02  
**Decision:** Robinhood transports use an explicit read/safe-review allowlist. Every unknown or
mutating tool—including watchlist and scan mutation, not only order placement—is unavailable in
BROKER_SHADOW and denied before network transmission.  
**Alternatives:** Block only `place_option_order`/`cancel_option_order`.  
**Reason:** Current Robinhood MCP exposes additional state-mutating tools, and future capability
discovery may find more. Default-deny prevents schema growth from silently widening authority.

## ADR-006 — Conservative local-model context on 8 GiB VRAM

**Date:** 2026-09-02  
**Decision:** Benchmark `qwen3.5:9b` initially at 4,096 context tokens and concurrency one, record
the immutable model digest, and increase context only after measured headroom.  
**Alternatives:** Use the model-advertised 256K context.  
**Reason:** The published 6.6 GB Q4_K_M weights leave limited KV-cache headroom on an RTX 3060 Ti.

## ADR-007 — Append-only execution journal with database identity defense

**Date:** 2026-09-03  
**Decision:** Implement `PostgresExecutionStore` over the existing environment audit tables.
Keep immutable evidence retry-idempotent, append order/position state snapshots, and enforce
semantic execution identities with PostgreSQL unique indexes. Add `append_sequence` identity
columns and use namespace-scoped, keyset-paginated reads.  
**Alternatives:** In-memory execution state; scanning a fixed number of recent rows; sorting state
by the domain model's `created_at`; relying only on a Python lock.  
**Reason:** Business timestamps can remain identical throughout replay or an order lifecycle.
Insertion order must be independent, startup must not silently omit old unresolved evidence,
and competing database writers must not create conflicting durable intents. The local lock
serializes one store instance; the database indexes arbitrate immutable identity races.

Migration `0002_durable_execution` includes the shared, Demo, and Live audit schemas. It does not
delete duplicate evidence to make an upgrade pass. Newly assigned sequence values cannot recover
unrecorded legacy tie chronology. Real PostgreSQL migration/concurrency/recovery validation is
still required; mock SQL checks are not a substitute.

## ADR-008 — Persist the simulated ledger and replay checkpoint together

**Date:** 2026-09-03  
**Decision:** Store a versioned, checksummed `OfflineRuntimeSnapshot` containing `LedgerSnapshot`
and `OfflineReplayCheckpoint` in one `shadow_ledger_events` payload. Restore only the same fixture
and namespace, then reconcile orders/fills/positions with the durable execution journal before
enabling entries.  
**Alternatives:** Restore cash alone; persist the replay cursor independently; blindly repeat
submission after a crash.  
**Reason:** A newer ledger snapshot may survive while its replay group has not yet checkpointed.
The group may repeat, but stable fill/intent identities and exact-command correlation make that
recovery auditable. Missing or conflicting durable evidence must stop reconciliation, not be
interpreted as an empty account. These snapshots never become Live brokerage state.

## ADR-009 — Finite replay with separate scheduling and trading clocks

**Date:** 2026-09-03  
**Decision:** `OfflineReplaySession` advances an injected virtual clock through one bounded
timestamp group per scheduled step. Wall time schedules workers; replay time owns quote ages,
fills, candidate evidence, and approval expiry. A finished fixture disables new entries and is
not automatically looped.  
**Alternatives:** Present historical quotes as a live feed or restart the fixture when exhausted.  
**Reason:** Repetition must not manufacture fresh observations or qualification sessions. Failed
callbacks/persistence do not advance the durable group checkpoint; retry/recovery uses stable
identities and explicitly idempotent consumers.
