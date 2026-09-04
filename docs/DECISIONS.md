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

## ADR-010 — Choose the model that passes measured routine latency

**Date:** 2026-09-03
**Decision:** Change the checked-in routine model to `qwen3.5:4b`; retain 9B installed
and preserve both benchmark runs. Keep model-independent trading/risk code.
**Alternatives:** Keep 9B based only on its earlier faster run or slightly higher
fixture-calibration score.
**Reason:** The final 100-case-per-model run measured p95 18,580 ms for 9B and 9,107 ms
for 4B. Both passed quality thresholds, but only 4B met the 15,000 ms routine latency
gate. See `MODEL_BENCHMARK.md` for evidence and limitations.

## ADR-011 — Count pending admission and require explicit journal repair

**Date:** 2026-09-03
**Decision:** Reserve aggregate option risk, position slots, cash, and daily entry
admission when a simulated entry is accepted, before a fill. Cancellation does not
refund the daily admission budget. Serialize ExecutionService admission and recheck
mode, approvals, kill switches, and evidence before transmission.
**Alternatives:** Count filled positions alone; rely on a periodic health poll.
**Reason:** Multiple pending orders could otherwise exceed risk limits. A failed
fill/position journal write latches execution unhealthy until explicit successful
reconciliation; healthy broker/database probes alone cannot clear it. An empty
open-order list is not sufficient negative evidence for a timed-out live submission.

## ADR-012 — Separate bounded current-source ingestion from replay

Date: 2026-09-03.

Decision: only explicitly enabled, validated public feeds are polled outside OFFLINE_SIM.
Store source observations before stable environment-scoped events, repair interrupted event
persistence from the durable source records, and retain ambiguous revisions without choosing
an unsupported "latest" claim. Unknown publication time never becomes inferred UTC.

Alternative rejected: poll all example URLs automatically or merge current headlines into
historical replay. Those approaches conceal missing coverage and violate causal evidence.
Only the Federal Reserve feed is currently enabled. This does not complete EDGAR, issuer
mapping, current-market consumption, or authenticated broker integration.

## ADR-013 — Deadline expiry is an unresolved operation, not permission to retry

Date: 2026-09-04.

Controller health and reconciliation have strict boolean results and monotonic deadlines.
Timed-out operations latch HALTED and cannot restore health with a late result. A cancelled
execution may have transmitted bytes; preserve unresolved journal evidence and block further
writes. Shutdown cannot release its instance lock until all tracked callbacks terminate.
The alternative of detaching timed-out callbacks and starting another controller could create
concurrent writes. A process exit releases the OS lock, but restart still requires reconciliation.

## ADR-014 — Shared federal registry records are immutable reference revisions

Date: 2026-09-04.

Keep the existing shared table, append revisions, and serialize parent-check/append in one
PostgreSQL transaction with an advisory lock. The server supplies record time and actor;
reference API access always requires the local token. Availability is distinct from publication,
and historical queries cannot see newly recorded reference knowledge. Source-host approval and
an operator verification timestamp are not automatic fact verification. No reference score
changes hard risk or broker authority. See `FEDERAL_REGISTRY.md`.

## ADR-015 — Private operational storage must be shared across launch contexts

Date: 2026-09-04.

Use `%USERPROFILE%\.options-sentinel` outside OneDrive and packaged LocalAppData. Filesystem
handle evidence proved MSIX redirected the old configuration; the scheduled task could not
read the same logical path. Explicit migration copies existing credentials exactly and leaves
the source intact. Do not regenerate credentials or weaken ACLs to solve path virtualization.
Preserve both Docker disks and inspect a verified copy before any old-data recovery.

Operational backups use pg_dump's consistent archive and verify that exact archive in an
exclusively created, ownership-tagged disposable database. They never restore over the running
database. No automatic retention deletion or misleading claim of recovered historical data.
