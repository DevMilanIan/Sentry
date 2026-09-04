# Operations

## Safety status

The checked-in configuration is `DEMO / OFFLINE_SIM / RESEARCH` with
`runtime.environment_execution_disabled: true`. The persistent service therefore remains
fail-closed even when healthy. `config/live.yaml` has external-write authority disabled and
starts `HALTED`. Neither a mode change, a funded account, nor a passing offline test grants
external-write authority.

As of September 3, host setup is not complete: WSL2/Docker and a running PostgreSQL deployment
are unverified, and Windows Time is stopped. Ollama and both benchmark models are installed.
See `docs/MACHINE_AUDIT.md` for evidence and the elevated setup/reboot steps. No broker OAuth,
funding, account-backed shadow qualification, or live activation has been performed.

## Credential-free verification

From the repository root, the existing virtual environment can run:

```powershell
.\.venv\Scripts\python.exe -m app.main validate-config
.\.venv\Scripts\python.exe -m app.main demo-once
.\.venv\Scripts\python.exe -m pytest
```

`demo-once` is a bounded, causal entry/exit regression scenario using bundled fixtures,
in-memory audit storage, simulated fills, and a versioned scripted reasoning provider. Its
isolated scenario uses `AUTO` and reports that separately from the configured service mode.
It needs neither PostgreSQL, Ollama, nor Robinhood credentials; it does not change the persistent
service configuration. It is not evidence that PostgreSQL persistence, actual model reasoning,
real-time data, or broker-backed qualification has passed.

The installed Ollama models are benchmarked separately. The completed verified result is
`benchmarks/results/qwen35_2026-09-03_verified.json`; it recommends `qwen3.5:4b` under the measured
latency/resource gates. `config/model.yaml` and `.env.example` now select that model; check any
existing `.env` or process overrides. Keep model names and digests in evidence when changing
the configured model; rerun the benchmark after material model/runtime/hardware changes.

## Start and stop

The persistent `serve` command requires PostgreSQL. It runs mandatory Alembic migrations before
starting; database failure aborts startup rather than silently substituting memory. Offline
service reasoning uses the configured local Ollama endpoint, not the smoke scenario's scripted
provider. There is no cloud fallback.

After the host prerequisites and clock checks pass:

1. Create ignored `.env` from `.env.example` only if it does not already exist; preserve any
   existing local configuration. Set a long random `SENTRY_DASHBOARD_TOKEN` and an explicit
   strong `POSTGRES_PASSWORD`, replacing both example placeholders. Compose rejects a missing
   or empty database password, but does not detect an unchanged example placeholder. Prefer a
   long URL-safe password because Compose interpolates it into the database URL. Never commit
   or display the rendered secrets.
2. Keep the environment/backend/mode and execution-disable gate at the checked-in safe values.
   For Docker Desktop, the native Ollama endpoint is
   `SENTRY_OLLAMA_URL=http://host.docker.internal:11434`. Verify that the container can reach the
   local endpoint without exposing Ollama publicly. If it cannot, treat model health as failed;
   do not disable firewall protection to force connectivity.
3. Run `docker compose up --build -d` from the repository root, or use
   `scripts/windows/Start-Sentinel.ps1` after reviewing its host checks. The launcher checks
   Windows Time is running, but does not itself prove the clock offset is within 250 ms.
4. Inspect `docker compose ps`, local logs, the dashboard at `http://127.0.0.1:8000/`, and
   `/api/state`. `/health` returning 503 is expected while `HALTED`; a 200 is not sufficient
   proof that entries are enabled. Inspect environment, backend, safety reason, database,
   execution health, reconciliation, freshness, and unresolved-submission fields together.
5. Verify actual database-backed crash/restart recovery before unattended operation. The
   repository's injected/in-memory tests do not replace this deployment check.

Compose uses PostgreSQL 17 on a named volume, with no database port published to the host. The
application port is published only at `127.0.0.1:8000`. Configuration is mounted read-only and
`var/` is mounted for runtime state. `SENTRY_DATABASE_URL` is set by Compose from
`POSTGRES_PASSWORD`; a different URL in `.env` does not override that service-level setting.

Stop the application with `docker compose stop trading-app`; stop the full stack with
`docker compose down`. Do not add `--volumes` to normal shutdown: it removes database storage.
Stopping the controller does not cancel broker orders or flatten positions.

For a future native deployment with a verified database, use the same virtual-environment
interpreter and `-m app.main serve` from the repository root. Native Python does not automatically
load `.env`; supply the database URL, dashboard token, and model overrides through the process
environment. The native Ollama default is `http://localhost:11434`. The downloaded PostgreSQL
audit artifacts in `var/tools/` are not a provisioned native database.

## Finite offline replay

The persistent offline service consumes the configured fixture one available-timestamp group
per step. The default fixture is `app/market/fixtures/offline_e2e_session.json`. Wall time drives
supervision; a separate virtual trading clock controls quote ages, fills, and approval expiry.
The state API explicitly exposes replay metadata, both clocks, and `live_market_data: false`.

Ledger snapshots and replay checkpoints are persisted together with a checksum and namespace.
Restart resumes the recorded cursor and validates ledger orders against durable exact command
intents. A fixture/hash/namespace mismatch or unresolved intent blocks recovery. Do not edit the
fixture in place under an existing checkpoint or delete evidence to force replay.

The fixture does not loop. On exhaustion the runtime disables new entries and reports that no
future fills are available; existing simulated holdings need not be flat. A replay-completion
report is explicitly labeled as a finite fixture, not a real-market qualification session.
Keep independent replay experiments in separately configured test storage/namespaces. The
bounded `demo-once` scenario can be rerun without altering the persistent service's checkpoint.

## Emergency stop

Use the authenticated dashboard emergency-stop control or create the configured sentinel
`var/TRADING_DISABLED` relative to the repository root (`/app/var/TRADING_DISABLED` inside
Compose). Do not use the old repository-root `TRADING_DISABLED` path with the checked-in config.
The file survives restart because `var/` is mounted. The separate
`runtime.environment_execution_disabled` configuration gate also forces a halt.

The dashboard writes the file and enters `HALTED` before recording its database audit. If the
audit fails, the stop remains active even if the HTTP request reports failure. This control
does not cancel orders, close positions, revoke credentials, or terminate the process.

Removing a sentinel does not clear an in-process manual halt. The resume control releases an
entry-pause latch but does not override `HALTED`. Resolve and document the incident first;
any reviewed restart must still reconcile and pass fresh health evidence and the configured
30-second startup window. Never remove a safety gate merely to obtain a green dashboard.

## Environment changes

`DEMO`/`LIVE` and `OFFLINE_SIM`/`BROKER_SHADOW` cannot be changed in a running process. Stop the
process before a reviewed configuration change; validate the immutable binding before restart.
There is no implemented environment-switch CLI/script. Schema, runtime directory, and
idempotency namespace must match the selected profile. Only shared reference data and a
qualified external-account fingerprint may cross the boundary, not simulated cash/orders/fills.

The current composition does not infer an authenticated Robinhood adapter from profile names:
`BROKER_SHADOW` and `LIVE` remain disconnected/fail-closed without the staged connection work.
Broker shadow requires the intended real account's approved read/review connection while its
external-write firewall remains deny-all, even if funded. Offline fixture sessions do not count
toward the required five regular account-backed shadow sessions. Funding and live activation
remain separate explicit user gates after qualification; this runbook does not authorize them.

## Recovery

- After an unclean shutdown, the system starts `ENTRY_DISABLED` or `HALTED`, checks database and
  broker/ledger state, reconciles durable intents, and requires the health window. A running
  process is not proof of successful recovery.
- `SUBMISSION_UNKNOWN` or an intent with ambiguous execution history blocks dispatch. Preserve
  the exact command, idempotency key, ledger snapshot, and reconciliation evidence. Never blind
  retry or manually resubmit before establishing the outcome.
- A failed ledger/checkpoint write blocks further mutations until durable state can be restored
  and reconciled. Do not acknowledge a failed database write as a successful order.
- Instance lock files under `var/locks/` are intentionally retained. OS locks are released on
  process death; the presence of `demo.lock` or `live.lock` alone is not a stale-lock failure.
  Do not unlink a lock file to bypass another controller.
- Back up PostgreSQL with `pg_dump` into restricted local storage; never commit dumps. Test
  restoring into an isolated database before relying on the backup. Backup scheduling and a
  verified deployment restore are not established by the presence of these instructions.
- If broker authorization is added later, rotate OAuth/session material through the approved
  flow; never put it in prompts, logs, fixtures, or shared diagnostics.

## Optional logon startup

Only after host reliability and actual deployment recovery checks pass, review and run
`scripts/windows/Install-StartupTask.ps1` elevated. It registers a limited, interactive-user
logon task with duplicate-instance prevention and bounded restart attempts; it is not a
pre-logon Windows service. The task calls the fail-closed Compose launcher. Docker Desktop and
Ollama availability at logon still require verification. Use
`scripts/windows/Remove-StartupTask.ps1` to unregister that task if needed. Neither operation
is performed automatically by the application, and startup-task installation is not yet claimed.

## Diagnostics

Inspect `/api/state`, `/health`, `/metrics`, `/api/broker-command-intents`, `/api/notifications`,
and `/api/reports`. Controls require the local token in `X-Dashboard-Token` (or the dashboard
password field). Keep the dashboard loopback-only; read endpoints expose operational data.

JSON logs are written under the bound runtime directory, for example
`var/demo/offline_sim/logs/sentinel.jsonl`. Health events, local notification events, and
operational reports are also audited in PostgreSQL. Local notifications are not evidence of
email/SMS/push delivery. Replay reports use replay time and cannot establish current-market
uptime. See `docs/SECURITY.md` before granting LAN access or sharing diagnostic output.
