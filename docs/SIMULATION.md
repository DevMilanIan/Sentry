# Simulation and replay semantics

`OFFLINE_SIM` uses the same account/execution contract as broker-backed modes but consumes only an
injected market-data provider and injected clock. The simulator owns cash, orders, fills,
positions, P&L, cancellations, and expiration; it never invents market observations.

An order can use only observations whose effective timestamps are at or after submission and no
later than the current virtual time. The conservative fill model requires executable post-order
quote evidence through the limit and adequate observable size; merely touching an OHLC range is
not evidence. The optimistic model exists only for diagnostics. Model name/version, seed, event
IDs, and the reason for every transition are persisted.

Deterministic regression replay uses recorded model outputs and seeded fills. Reasoning replay may
rerun the configured local model but stores the new model/prompt/policy versions separately.
Rejected candidates use predefined horizons, never hindsight-selected favorable windows.

Known limitation: replay quality cannot exceed timestamp granularity, quote coverage, and retained
size/trade evidence. Results are hypothetical and exclude unobserved queue priority and fees unless
the selected fill model explicitly represents them.

## Implemented replay/runtime path — 2026-09-03

The September 2 bootstrap introduced the replay-first boundary. The implementation now includes:

- `app/market/replay.py`: causally filtered fixture-backed market observations.
- `app/sentinel/offline.py`: `OfflineReplaySession`, bounded timestamp groups, stable event
  identities, virtual-time freshness, and versioned fixture/namespace checkpoints.
- `app/broker/shadow_ledger.py` and `app/broker/simulated.py`: full ledger export/restore and a
  persistence callback after simulated state changes.
- `app/demo/runtime.py`: scheduling, exact execution-service dispatch, ledger/checkpoint
  persistence, and startup reconciliation.
- `app/execution/postgres_store.py`: append-only execution evidence and typed durable lookups.

A wall clock schedules the local workers and host-health checks. The injected virtual trading
clock controls historical availability, quote/account ages, order/fill times, and approval expiry.
The dashboard/report path explicitly identifies replay. Exhausting a finite fixture disables
entries; it does not loop historical observations or claim a current-market feed.

## Checkpoint and crash semantics

Each persisted `OfflineRuntimeSnapshot` contains both a `LedgerSnapshot` and an
`OfflineReplayCheckpoint`, with a content hash and startup namespace. The ledger retains cash,
orders and their exact commands, idempotency mappings, positions, fills, quotes, proposal/rejection
evidence, deposits, and P&L state. Restore rejects mismatched fixture/namespace or checksum data.

The checkpoint advances only after the timestamp group's downstream work succeeds. A crash can
leave a newer durable ledger snapshot alongside the last completed replay cursor. Recovery may
reprocess that unfinished group, so consumers use stable identities and idempotent persistence.
The runtime correlates ledger orders with both `OrderIntent` and `BrokerCommandIntent`; it records
recovery differences and synchronizes the execution journal rather than resubmitting an order.
Missing durable evidence, unknown submission, failed persistence, or unexplained state divergence
must block new entries.

Audit ingestion uses PostgreSQL `append_sequence`, because a broker order's original `created_at`
does not change on every state update and replay can emit many records at the same instant.
Orders remain append-only; position replacement publishes a complete snapshot marker after its
individual records. Startup scans page through evidence and fail on their maximum bound rather
than treating a truncated history as reconciled. Immutable intent/command/fill identities are
protected by database unique indexes, with identical retry content accepted by the store.

## Evidence and remaining limits

Focused store/repository tests and in-memory runtime crash/recovery tests pass, including the
pre-order crash gap, same-timestamp state ordering, idempotent fill persistence, empty position
snapshots, binding guards, and conflicting identity rejection. The migration and SQL filters have
mock/compiled-SQL tests only: a real PostgreSQL migration, concurrent-writer race, transaction
failure, process-kill recovery, and backup/restore exercise are still pending.

Legacy rows cannot recover an ingestion chronology that was never recorded. Historical replay
also cannot establish real execution quality, broker availability, authenticated account parity,
or a profitable strategy. WSL2/Docker and unattended deployment remain open. No replay run counts
toward the five real regular-market broker-shadow sessions, and none authorizes authentication,
funding, Live trading, or automatic promotion of trading mode.
