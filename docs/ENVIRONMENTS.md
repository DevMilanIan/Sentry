# Execution environments

| Environment | Backend | Broker reads | External writes | Effective account |
|---|---|---:|---:|---|
| DEMO | OFFLINE_SIM | None required | Impossible | Simulated ledger |
| DEMO | BROKER_SHADOW | Authenticated real reads/safe reviews | Denied before transport | Separate ShadowLedger |
| LIVE | Robinhood MCP | Authenticated | Gated and disabled by default | Fresh broker state |

The environment, DEMO backend, database schema, runtime directory, and idempotency namespace are
bound exactly once at process startup. Changing one requires a controlled stop, explicit local
selection, an audit record, restart, health window, and reconciliation.

BROKER_SHADOW and LIVE intentionally reuse only the protected authorization and fingerprint of the
same external Agentic account. Shadow cash, positions, approvals, orders, fills, P&L, command
intents, and outcomes never migrate. Live reconciles from the broker as a new state domain.

`DEMO_EXPLORATORY` increases lifecycle coverage after deterministic minimums pass.
`LIVE_CONSERVATIVE` adds stronger qualitative requirements. Neither profile can alter hard risk or
execution authority.

## Implemented bindings and persistence — 2026-09-03

The September 2 bootstrap established the profiles. The September 3 implementation binds the
repository and execution store to these checked-in defaults:

| Profile | Database schema | Idempotency namespace | Runtime artifacts |
|---|---|---|---|
| DEMO/OFFLINE_SIM | `demo` | `demo-offline-sim` | `var/demo/offline_sim` |
| DEMO/BROKER_SHADOW | `demo` | `demo-broker-shadow` | `var/demo/broker_shadow` |
| LIVE | `live` | `live` | `var/live` |

Both Demo backends share the Demo schema but not their namespace. `PostgresAuditRepository`
payload queries filter environment and namespace in addition to schema translation.
`PostgresExecutionStore` checks record bindings on writes and typed reads. Notification/report
models reject `LIVE` with a Demo backend and reject a Demo label without a backend.

Shared reference tables remain separate from account/trading state. Broker-observed state and
the effective simulated/shadow account are distinct typed snapshots even when recorded under the
same Demo qualification run. A shared credential or fingerprint is not permission to reuse a
Demo approval, intent, fill, position, or balance in Live.

Migration `0002_durable_execution` adds ingestion sequence and filtered unique execution-key
indexes to the existing schema layout. Same-timestamp replay writes are ordered by ingestion,
not by business time or a random UUID. Identical immutable retries are accepted by the store;
changed collisions fail closed. Existing duplicate identities require investigation rather than
automatic deletion. Apply and verify migrations against real PostgreSQL before relying on this
path outside mocked/in-memory tests.

## Recovery boundary and current limits

The offline runtime stores a checksummed ledger and replay checkpoint together, restores only a
matching namespace/fixture, and compares reconstructed state with exact durable command evidence.
Unresolved intents or missing/conflicting ledger orders prevent successful reconciliation. A
restart is never an implicit environment change or an instruction to retry a broker write.

The profiles are still locked: default startup is `DEMO/OFFLINE_SIM + RESEARCH`, the environment
execution-disable setting is enabled, and the Live profile has no external-write authority and
starts `HALTED`. WSL2/Docker provisioning, real PostgreSQL recovery validation, and unattended
deployment remain pending. Authenticated same-account broker-shadow sessions, deliberate funding,
and explicit Live activation have not been completed; configuration/code availability is not
qualification evidence or Live permission.
