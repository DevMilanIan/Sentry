# Development log

## 2026-09-02 — Repository bootstrap

- Received and reviewed the implementation specification.
- Confirmed the workspace was empty and initialized a new Git repository.
- Established safe-default configuration: `DEMO/OFFLINE_SIM + RESEARCH`, no external-write
  authority, localhost-only dashboard, and environment-isolated persistence names.
- Began read-only host audit and revalidation of dynamic platform facts.
- External blockers expected later: user-owned Robinhood OAuth, five real regular-market
  broker-shadow sessions, subsequent user funding/capital ceiling, and explicit Live mode
  authorization.

## 2026-09-02 — Phase 0 audit and platform revalidation

- Hardware is suitable: Windows 11, Ryzen 5 5600X, 64 GiB RAM, RTX 3060 Ti 8 GiB, ample NVMe.
- WSL2/Virtual Machine Platform are disabled; Docker and Ollama are absent.
- Found a safety-critical host clock offset of approximately -5.85 seconds with Windows Time
  stopped/manual. Timestamp-sensitive operation remains blocked pending remediation.
- Revalidated Robinhood, MCP SDK v2.1.1, Ollama/Qwen, and Docker/WSL facts against primary sources.
- Updated architecture to default-deny all unknown/mutating MCP tools and pin SDK v2.1.1.
- Created a CPython 3.12 virtual environment and installed development dependencies.

## 2026-09-03 — Core implementation and local verification

- Continued the September 2 bootstrap without treating the repository scaffold as completion.
  Implemented typed configuration/domain models, injected clocks, deterministic market replay,
  candidate/contract selection, hard risk checks, bounded local reasoning, simulated and
  broker-shadow adapters, exact execution intents, the write firewall, position monitoring,
  qualification evaluation, and localhost control/reporting modules.
- Added focused reporting, notification, and learning tests. The 70-case focused run passed,
  with 100% line coverage of those three modules at that run. The tests exposed and drove fixes
  for invalid environment/backend labels, non-UTC report timestamps, unknown-uptime formatting,
  and prohibition changes hidden in alternate or nested learning-proposal paths.
- Retained the local model benchmark artifact
  `benchmarks/results/qwen35_2026-09-03.json`: the recorded 100-case `qwen3.5:9b` run reports
  100% valid JSON, 99% grounding, and a 12.581-second p95 latency. This is bounded fixture evidence,
  not a profitability result or authorization to trade.

## 2026-09-03 — Durable execution journal and restart recovery

- Added `app/execution/postgres_store.py`. `PostgresExecutionStore(repository, clock)` implements
  the execution-store contract and typed proposal, approval, risk, review, intent, order, fill,
  position, transition, and reconciliation retrieval. It contains no broker transport authority.
- Added environment-and-namespace-scoped `find_payload`/`list_payloads` queries with keyset
  pagination. Startup scans fail closed if the configured bound is exceeded instead of accepting
  silently truncated order evidence.
- Added migration `0002_durable_execution`: audit-table `append_sequence` identities establish
  ingestion order independently of replay timestamps, and filtered unique indexes defend immutable
  execution identities. Identical retry content is a no-op; changed identity content is rejected.
  Existing duplicates are not deleted automatically. Legacy equal-timestamp history cannot gain
  chronology that was never recorded and needs reconciliation before reliance on migrated state.
- Added serializable `LedgerSnapshot` integration and the finite `OfflineReplaySession` runtime.
  `app/demo/runtime.py` persists a checksummed ledger-plus-replay-checkpoint envelope in
  `shadow_ledger_events`, verifies its binding on restore, and correlates restored orders with
  durable order and command intents. A crash gap is reconciled, not used to resubmit an order.
- Verification for the new store/repository work: 29 focused unit/contract cases passed. The
  combined selection including execution-service and in-memory runtime recovery/guard tests
  passed 38 cases. Ruff and strict mypy passed for the six owned production/migration/test files.
  SQL was compiled and migration statements inspected with mocks; this is not a real PostgreSQL
  migration, concurrent-writer, backup/restore, or process-kill integration test.

## 2026-09-03 — Verified model selection and further safety hardening

- Completed the second real Ollama benchmark artifact,
  `benchmarks/results/qwen35_2026-09-03_verified.json`, with 100 cases per model. Both models
  produced 100% valid JSON and 99% allowed-fact-ID grounding. The 9B p95 was 18.580 seconds,
  exceeding the 15-second limit; 4B p95 was 9.107 seconds. Selected `qwen3.5:4b` in the default
  model configuration and environment example. These bounded fixture results do not prove
  semantic entailment, trading quality, or profitability. See `MODEL_BENCHMARK.md`.
- Fixed reservation accounting for pending entries, journal-failure reconciliation latching,
  and proposal dispatch pagination. Durable command preparation now precedes order-intent
  persistence when deterministic capability validation can still reject the command. A locally
  denied pre-submission intent is accepted as non-submission evidence only with its exact
  persisted transition; missing broker orders alone never authorize a retry.
- Hardened Robinhood read/review response validation: unknown/ambiguous account state, tool
  catalogs, collections, and review acceptance fail closed. The public account/portfolio tools
  use a trusted, explicitly selected Agentic account adapter rather than a guessed response
  mapping. No real response schema or account selection has been authenticated yet.
- Added shadow-ledger restart snapshots, exact command/idempotency restoration checks,
  failed-persistence mutation blocking, optional account pinning, and an explicit immutable
  acknowledged-terminal-history baseline. Active or unacknowledged broker orders still fail
  qualification. Production composition must persist the account/baseline envelope separately
  from local ledger state; this is not yet a running broker-shadow service.
- Added a concrete official MCP 2.1.1 read-only session and protected OAuth storage bridge.
  Its tool names, endpoint, discovery bounds, uncached reads, and noninteractive authorization
  failure path are tested with mocks. Existing valid protected credentials are required before
  network creation. No OAuth was run, no real credentials were created, and unattended refresh,
  authenticated account mapping, market data, and runtime composition remain open.
- Added Windows-only runtime `pywin32`; installed version 312 and `win32crypt` import verified.
  A fixture-only DPAPI roundtrip passed. Documentation distinguishes DPAPI encryption from
  separately verified NTFS ACLs and directs future credential storage outside OneDrive.

## 2026-09-03 — Source ingestion, calendar, and verification checkpoint

- Added public-feed polling outside OFFLINE_SIM with explicit source opt-in, causal source
  provenance, stored hashes, stable durable events, and rolled-feed crash-gap repair. Review
  exposed four issues and drove fixes: whole-response deadlines, malformed-item error
  classification, durable repair independent of the latest feed, and deterministic revision
  retention. Only the verified Federal Reserve feed is enabled; the bounded read returned 20
  documents. `CATALYST_INGESTION.md` records the incomplete broader-source and EDGAR coverage.
- Verified the NYSE published 2026–2028 schedule, added early closes to phase/reporting/EOD exit
  timing, and fixed Saturday New Year and pre-2022 Juneteenth rules. Session-close/phase requests
  outside verified years fail explicitly. Calendar coverage is scheduled equity-session data,
  not evidence of unscheduled closures or individual option trading cutoffs. See
  `MARKET_CALENDAR.md`. Explicit simulation expiration-event timing remains a separate
  limitation; the calendar change does not establish broker settlement semantics.
- Added ten opt-in real-PostgreSQL tests for isolation, ingestion order, uniqueness/concurrency,
  rollback, read-only health, and runtime restart. They create ownership-tagged disposable
  schemas only. All ten remain skipped because `SENTRY_TEST_DATABASE_URL` is unset; actual
  Alembic migration and deployment backup/restore remain unverified.
- Aggregate verification at this checkpoint: **447 tests passed, 10 real-PostgreSQL tests
  skipped**. Ruff passed repository-wide; strict mypy passed all 88 application files. One
  third-party Starlette/AnyIO deprecation warning remains. The first aggregate run exposed a
  stale test expectation for 9B; the test now checks the measured 4B default and clears model
  environment overrides. `validate-config` and the credential-free causal entry/exit smoke
  scenario passed; its stable journal hash remains
  `fb2143e226b044a17902e48ed620e22a9686ccf3e55e0eae72b75c65aa05d1ff`.
- Fresh read-only host check: process is not elevated; timezone is Eastern Standard Time;
  Docker is absent; Windows Time is stopped/manual. No privilege boundary was bypassed, no
  unverified PostgreSQL executable was run, and no host reboot or financial action was taken.

## Remaining setup and qualification work

Latest host update: see the authorized provisioning section below and `SETUP_RESUME.md`;
the following original remaining-work list predates the late-evening installs.

- WSL2/Ubuntu and Docker provisioning remain open; no unattended WSL/container deployment has
  been demonstrated. Historical host-clock findings remain in `MACHINE_AUDIT.md`; fresh clock and
  startup evidence are required before timestamp-sensitive broker qualification.
- Apply and test the Alembic chain against real PostgreSQL, including uniqueness races,
  failed commits, crash/restart, and backup/restore. Test the migration against retained data
  before upgrading any non-disposable environment.
- Continue end-to-end runtime and fault verification. Finite historical replay, passing unit
  tests, and local model benchmarks do not constitute five authenticated regular-market sessions.
- No broker authentication, funding request, real order submission, or Live activation has been
  performed by this work. Same-account broker-shadow qualification, user-owned funding/capital
  decisions, and explicit Live activation remain staged gates. V1/full-project completion is not
  claimed.

## 2026-09-03 — Authorized host provisioning and reboot checkpoint

- User reported automatic time and authorized PC installs/commands, then explicitly approved
  reboot after installations. Rechecked actual state: clock offset had improved to ~150 ms,
  but Windows Time was still stopped/manual and the current process was not elevated.
- Used normal UAC elevation. The Windows PowerShell 5 helper exited under its Restricted
  policy without making changes. Verified Microsoft-signed PowerShell 7.6.5 and used its
  existing policy; no global execution policy was changed. Clock service setup/resync passed,
  offsets measured 136–147 ms, AC sleep was disabled, and both Windows features enabled with
  restart required. The caller itself remains unelevated; elevated helpers ended successfully.
- Installed Python 3.12.10 per-user. Downloaded the official Docker Desktop 4.89.0 installer,
  checked WinGet's exact SHA256 and Valid Docker Inc Authenticode, and completed documented
  per-user installation (installer exit 0). CLI 29.7.2 and Compose 5.5.0 verified; engine remains
  stopped. WinGet's current Docker entry does not detect this per-user installation, so the
  dependency script now checks installed paths before attempting another installation mode.
- Upgraded existing WSL 2.3.26.0 to 2.7.12.0 using a separately elevated WinGet helper after
  the unelevated update correctly returned administrator-required. Installed the Ubuntu 24.04
  LTS package; no distribution was initialized before the required host reboot.
- Added a source-whitelist `.dockerignore`, separate opt-in verification build target/profile,
  and actual Alembic fresh/repeat smoke test in a uniquely created ownership-tagged disposable
  database. No database port is published and production `sentinel` is not migrated by that
  test. Unsupported custom production schema overrides now reject before migration.
- Final pre-reboot checks: **461 passed, 11 actual-PostgreSQL tests skipped**; Ruff/mypy clean;
  setup scripts parsed. No `.env`, database credentials, broker credentials, native database,
  running container stack, or startup task was created. `SETUP_RESUME.md` contains exact paths
  and remaining post-reboot checks. No brokerage or trading authority changed.

## 2026-09-04 — Post-reboot deployment and actual recovery

- Confirmed completed reboot. Initialized Ubuntu 24.04 under WSL2, created the locked-password
  non-sudo local user `sentinel`, and selected it as distribution default. Docker Desktop's
  Linux engine runs; no second Docker engine was installed inside Ubuntu.
- Initial postboot Windows Time offset was approximately 744 ms despite service readiness.
  Used the authorized normal UAC flow to set bounded high-accuracy polling/correction values,
  retaining previous settings in ignored local evidence. No phase-safety bounds, firewall, or
  security protections were disabled. Actual offsets improved to 13–22 ms, then 2–9 ms.
- Created independent 256-bit database/dashboard secrets under restricted LocalAppData NTFS
  storage, not the OneDrive repository. Existing values are preserved and never printed.
  Fixed PowerShell 7.6/.NET null-environment removal: a present empty value overrides Compose's
  private env file, so the launcher now uses actual environment-variable deletion and restores
  absent/empty/nonempty prior values distinctly.
- Actual PostgreSQL verification: 15 checks passed, including fresh/repeated Alembic migrations,
  transaction/isolation/uniqueness cases, durable runtime restart, and real custom-format
  pg_dump/pg_restore with exact filled-ledger/journal comparison in owned disposable databases.
  Deployed database remains on its own named volume, with no published host database port.
- Started the actual offline application at `127.0.0.1:8000`; database and native Ollama health
  passed. Finite replay completed at sequence 5 with fixture hash
  `726d119cb2b6563828eb59f734fd461ffada80eb78d4bef3dc3b9f2a567042dc` and simulated cash $25.
  It remains DEMO/OFFLINE_SIM/RESEARCH/HALTED, with no external-write authority and no positions.
- Actual app SIGKILL/restart preserved that exact cursor/hash/cash and reconciled successfully.
  A controlled database stop was detected after 14 seconds; HALTED remained active. Restored
  PostgreSQL in a finally block, then used authenticated local reconciliation to clear the
  offline persistence latch. Trading was not resumed; historical incident errors are retained.
- Full Linux testing exposed Alembic `fileConfig` disabling existing notification loggers and
  replacing application handlers. Embedded migrations now preserve logging; CLI Alembic keeps
  existing loggers enabled. Regression checks verify handler continuity across real migrations.
  Follow-up full container run: 484 passed, 20 host-specific cases skipped (Windows DPAPI and
  PowerShell parser cases), one third-party Starlette/AnyIO deprecation warning. Further new
  safety/startup changes below require their own final aggregate verification.
- This is an offline deployment/recovery milestone, not V1 or full-project completion. No
  Robinhood OAuth, real market/account connection, broker-shadow session, funding, or real trade
  has occurred. Runtime/source/federal/evaluation composition gaps and future user gates remain.
