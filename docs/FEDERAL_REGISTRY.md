# Federal reference registry

This registry is manually maintained, append-only research reference data. No factual records
are seeded by setup. It does not fetch URLs, authenticate with a broker, change risk limits, or
grant trading authority. The existing `shared.federal_relationships` table stores typed revision
envelopes, including actor, reason, immutable revision ID, stable relationship ID, previous revision
ID, and a canonical payload digest. The digest detects accidental corruption, not a malicious
database administrator who can replace both payload and digest.

## Local API wiring

The production runtime includes
`create_federal_registry_router(repository, wall_clock, authorize_reference, actor="local-dashboard")`
in the existing FastAPI application. It explicitly passes the wall clock, not OFFLINE_SIM's
virtual trading clock. A separate mandatory-token authorization callback protects every route,
even when ordinary dashboard controls are configured without tokens. The bound actor is
server supplied, not accepted from the request body. A shared dashboard credential establishes
local control authority, not the identity of a particular human operator.

| Route | Purpose |
|---|---|
| `GET /api/federal/relationships` | Latest causal reference snapshots; status labels and cursor pagination |
| `POST /api/federal/relationships` | Create a relationship with a complete `relationship` draft and `reason` |
| `PUT /api/federal/relationships/{id}` | Append a replacement draft with `expected_revision_id` and `reason` |
| `GET /api/federal/relationships/{id}/history` | Newest-first immutable revision history with a sequence cursor |
| `GET /api/federal/score/{ticker}` | Existing deterministic exposure score, eligible/excluded relationship IDs |
| `GET /api/federal/policy` | Current reference policy; there is no policy mutation or hard-risk endpoint |

There is no delete route. Deactivation is a new revision with `active=false`; old evidence remains.
On HTTP 409, reload the latest revision before deliberately resubmitting the edit. PostgreSQL
serializes writers across connections/processes with a relationship-keyed transaction advisory
lock, then compares the expected parent and appends in that same transaction. Lock acquisition
times out after five seconds. Direct database writes bypass this service and are unsupported;
incomplete, branched, legacy-unversioned, or corrupt stored chains fail closed instead of being
quietly interpreted as current evidence. In-memory locking is only for process-local tests.

## Evidence and causal boundaries

The server supplies revision `created_at`; clients cannot backdate that field. A draft supplies
source availability and last-verification times, but neither may exceed server time and verification
cannot precede availability. Dates and timestamps are checked before persistence. Even when a
source was published earlier, a new manual record is not visible to an `as_of` snapshot until its
server-recorded revision time. Preserve the returned `as_of` value across paginated snapshot calls.

`VERIFIED_REFERENCE` means the authenticated operator recorded a recent verification; it is **not**
an automated confirmation of the source's contents or the factual claim. URLs must use credential-free
HTTPS on the standard port and an explicitly approved source host. Default hosts are named federal
sources; an issuer's own primary-source host requires a separately reviewed reference-policy
configuration. A matching host is not proof that the document supports the asserted relationship.
No arbitrary URL is fetched. Inactive, ended, not-yet-effective, stale, unverified, or no-longer-approved
sources are labeled and excluded from the score. End dates are inclusive; default verification
freshness is 90 days. Reference-policy version is returned with every snapshot and score; historical
evaluation should pin that policy version separately.

Page size is 1–200. History returns `next_before_sequence`; snapshots return
`next_after_relationship_id`. Full causal reconstruction has an explicit default 10,000-revision
scan ceiling and fails visibly if exceeded. A narrow relationship history can still be paged when
the global registry exceeds that ceiling. This is not a silent first-page-only score. No score
overrides liquidity, contract quality, deterministic risk, or execution safety.

## Candidate-scoring integration

`CandidateResearchWorker(..., federal_registry=FederalRegistryService(...))` is the explicit
composition point. The registry must use the same runtime binding; the runtime supplies the wall
clock and repository. For each event, the worker queries the registry at exactly
`event.created_at`, not at the later processing time. Registry revisions recorded after that cutoff
cannot enter the packet.

The existing `federal_exposure` surveillance component receives the versioned 0–100 score. A
missing registry injection leaves the component missing and therefore zero. An injected registry
with only stale, inactive, ended, future-effective, or unverified entries supplies an explicit zero.
Only `VERIFIED_REFERENCE` relationships become full candidate facts; excluded relationship IDs
remain in the aggregate score fact so the exclusion is auditable without presenting an unverified
claim to the reasoning model. Fact values carry relationship/revision IDs, revision digest, primary
source URL, evidence status, policy version, score version, and the exact as-of time.

Candidate packets are limited to 16 registry relationships by default and fail that research
attempt visibly if the bound is exceeded. Custom feature providers cannot supply or override
`federal_exposure`; the registry is the sole source of that component. This feature remains
research-only and adds no broker, execution-service, approval, risk-limit, or hard-risk authority.
