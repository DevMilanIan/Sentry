# Live graduation gates

All gates are fail-closed. A missing measurement is a failure, not a pass. This file records
criteria; it cannot itself authorize execution.

| Gate | Objective evidence | Current state |
|---|---|---|
| Offline end-to-end parity | DEMO/OFFLINE_SIM lifecycle and deterministic replay pass | Local fixture/runtime tests pass; durable deployment pending |
| Replay causality | No-lookahead tests and identical seeded replay hashes | Automated fixture tests pass |
| Risk authority | Unit/safety suite proves configured rules | Local tests pass, including pending-order admission |
| Restart reconciliation | Clean/unclean restart and open/partial order scenarios pass | In-memory recovery tests pass; real process/host tests pending |
| Unknown submission | No blind retry; authoritative negative evidence required | Mock tests pass; real history/correlation evidence pending |
| Duplicate prevention | Durable fingerprints and broker reconciliation tests pass | Store tests/SQL contracts pass; PostgreSQL races pending |
| Environment isolation | Demo artifacts cannot be queried/used by Live repositories | Mock/SQL predicate tests pass; real schemas pending |
| Environment immutability | No in-process environment/backend switch path | Config/control tests pass |
| Stale-data behavior | Entry denied for stale quote/account/provider state | Automated tests pass |
| Kill switches | File/config/dashboard/environment and service-stop paths pass | Code tests pass; deployed service-stop test pending |
| Database recovery | Migration, write-failure, backup/restore evidence | Blocked on real PostgreSQL/host provisioning |
| Model output safety | Invalid/ungrounded JSON cannot reach proposal execution | Schema/reference tests pass; semantic grounding remains limited |
| Broker-shadow identity | Intended Agentic account fingerprint recorded | Blocked: OAuth later |
| Broker-shadow capability parity | Required live reads/reviews and schemas observed | Blocked: OAuth later |
| Broker-shadow write firewall | Every place/cancel/replace path denied pre-network | Mock tests pass; actual capability qualification pending |
| Exact command completeness | 100% schema-valid/reconstructable command intents | Fixture commands pass; authenticated schemas pending |
| Account-state separation | Real observed and shadow effective state never conflated | Mock tests pass; authenticated parity pending |
| Qualification duration | Five regular authenticated market sessions | Blocked: elapsed sessions |
| Qualification incidents | No unresolved safety-critical incident | Pending |
| Same-account continuity | Live fingerprint equals qualified fingerprint | Blocked: post-qualification |
| Explicit capital ceiling | User confirms amount after qualification | Blocked: user-owned action |
| Explicit Live activation | User selects initial `APPROVAL` after all gates | Blocked: user-owned action |

The minimum broker-shadow burn-in is five regular sessions and must include normal pre-market,
regular-market, EOD, restart/reconciliation, transient provider failure, and authenticated MCP
reconnect evidence. Profitability is not a graduation criterion. `EXIT_AUTO` and `AUTO` require
separate later user activation and additional real-operation evidence.

Updated 2026-09-03. No row in this document grants authority. Local simulated results,
compiled SQL, and model benchmarks do not close the real-database, host, authentication,
five-session, funding, or Live activation gates. V1/full-project completion is not claimed.
