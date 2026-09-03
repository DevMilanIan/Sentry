# Operations

## Safety status

The checked-in profiles cannot place a real order. `config/live.yaml` has external-write authority
disabled and starts `HALTED`. Do not modify those gates casually.

## Start and stop

1. Copy `.env.example` to ignored `.env` and replace local passwords/tokens.
2. Run `docker compose up --build -d`.
3. Check `http://127.0.0.1:8000/health` and the dashboard.
4. Stop with `docker compose stop trading-app`; stop the full stack with `docker compose down`.

For a native offline verification run, use `python -m app.main demo-once` after installing the
project. The run uses bundled fixtures and never opens a Robinhood connection.

## Emergency stop

Create `TRADING_DISABLED` in the repository/runtime working directory or use the authenticated
dashboard emergency-stop control. The process moves to `HALTED`. A restart does not clear the
sentinel file. Removing the file is insufficient by itself to resume after a fault; reconciliation
and the startup health window must pass.

## Environment changes

`DEMO`/`LIVE` and `OFFLINE_SIM`/`BROKER_SHADOW` cannot be changed in a running process. Stop the
process, use the audited environment-switch script, start the new profile, then reconcile. Only
shared reference data and a qualified external-account fingerprint may cross the boundary.

## Recovery

- After an unclean shutdown, the system starts `ENTRY_DISABLED` or `HALTED`, checks database and
  broker state, reconciles durable intents, and waits through the health window.
- `SUBMISSION_UNKNOWN` must be reconciled against broker state. Never manually resubmit it first.
- Back up PostgreSQL with `pg_dump`; never commit dumps. Test restore into an isolated database.
- Rotate OAuth/session material through the approved client flow; never paste secrets into logs.

## Diagnostics

Inspect `/health`, `/metrics`, recent JSON logs under the environment-specific `var/` directory,
and database health events. See `docs/SECURITY.md` before granting LAN access.

