# Live graduation gates

All gates are fail-closed. A missing measurement is a failure, not a pass. This file records
criteria; it cannot itself authorize execution.

| Gate | Objective evidence | Current state |
|---|---|---|
| Offline end-to-end parity | DEMO/OFFLINE_SIM lifecycle and deterministic replay pass | Pending |
| Replay causality | No-lookahead tests and identical seeded replay hashes | Pending |
| Risk authority | Unit/safety suite proves every configured rule | Pending |
| Restart reconciliation | Clean/unclean restart and open/partial order scenarios pass | Pending |
| Unknown submission | No blind retry; reconciliation evidence passes | Pending |
| Duplicate prevention | Durable fingerprints and broker reconciliation tests pass | Pending |
| Environment isolation | Demo artifacts cannot be queried/used by Live repositories | Pending |
| Environment immutability | No in-process environment/backend switch path | Pending |
| Stale-data behavior | Entry denied for stale quote/account/provider state | Pending |
| Kill switches | File/config/dashboard/environment and service-stop paths pass | Pending |
| Database recovery | Migration, write-failure, backup/restore evidence | Pending |
| Model output safety | Invalid/ungrounded JSON cannot reach proposal execution | Pending |
| Broker-shadow identity | Intended Agentic account fingerprint recorded | Blocked: OAuth later |
| Broker-shadow capability parity | Required live reads/reviews and schemas observed | Blocked: OAuth later |
| Broker-shadow write firewall | Every place/cancel/replace path denied pre-network | Pending |
| Exact command completeness | 100% schema-valid/reconstructable command intents | Pending |
| Account-state separation | Real observed and shadow effective state never conflated | Pending |
| Qualification duration | Five regular authenticated market sessions | Blocked: elapsed sessions |
| Qualification incidents | No unresolved safety-critical incident | Pending |
| Same-account continuity | Live fingerprint equals qualified fingerprint | Blocked: post-qualification |
| Explicit capital ceiling | User confirms amount after qualification | Blocked: user-owned action |
| Explicit Live activation | User selects initial `APPROVAL` after all gates | Blocked: user-owned action |

The minimum broker-shadow burn-in is five regular sessions and must include normal pre-market,
regular-market, EOD, restart/reconciliation, transient provider failure, and authenticated MCP
reconnect evidence. Profitability is not a graduation criterion. `EXIT_AUTO` and `AUTO` require
separate later user activation and additional real-operation evidence.

