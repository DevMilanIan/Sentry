# Options Sentinel

Options Sentinel is a local-first, event-driven options surveillance and execution platform.
It defaults to credential-free `DEMO/OFFLINE_SIM + RESEARCH`, uses deterministic risk and
execution controls, and cannot obtain real broker write authority from model output or a
dashboard request.

The system is intended for engineering and qualification of a very small experimental
account. It does not promise profitability and is not financial advice. Real trading remains
locked until account-backed broker-shadow qualification, explicit funding, and user activation
are all independently documented.

## Current status

Core offline lifecycle, continuous finite replay, durable execution-store code, restart
reconciliation, local reporting, and fault regressions are implemented. The measured routine
model is `qwen3.5:4b`; see `docs/MODEL_BENCHMARK.md`.

This is not yet an unattended production deployment or a completed V1. WSL/Docker and
real PostgreSQL verification need host provisioning. Broker-shadow authentication/schema
mapping, five real qualification sessions, and later user-owned Live gates remain open.

## Safe quick start

Requirements: Python 3.12+, or Docker Desktop/Compose. Ollama is optional for health checks and
reasoning; deterministic surveillance, replay, and position monitoring continue without it.

```powershell
if (-not (Test-Path -LiteralPath .env)) { Copy-Item .env.example .env }
# Set unique local secrets in .env; never commit it.
python -m venv .venv
.\.venv\Scripts\python -m pip install -e ".[dev]"
.\.venv\Scripts\python -m pytest
.\.venv\Scripts\python -m app.main demo-once
```

Or start PostgreSQL and the application:

```powershell
if (-not (Test-Path -LiteralPath .env)) { Copy-Item .env.example .env }
# Set POSTGRES_PASSWORD and SENTRY_DASHBOARD_TOKEN first.
docker compose up --build
```

The dashboard binds to <http://127.0.0.1:8000>. State-changing controls require the dashboard
token configured outside source control. See `docs/OPERATIONS.md` before running continuously.

## Safety boundary

- `DEMO` and `LIVE` are startup-only execution environments with separate persistence schemas.
- `BROKER_SHADOW` must initialize a deny-all external-write firewall before any MCP session.
- Every broker action first becomes an immutable `OrderIntent` and typed
  `BrokerCommandIntent`.
- Unknown submission outcomes are reconciled and never blindly retried.
- The local language model has no broker, shell, configuration, or risk-control authority.
- Presence of funds never grants trading permission.

See `docs/LIVE_GATES.md` for the deliberately unmet Live requirements.

## Verification checkpoint — September 3, 2026

447 tests pass; ten real-PostgreSQL cases are explicitly skipped pending a test database.
Ruff and strict mypy pass. The default is measured `qwen3.5:4b`, with trading still disabled.
Read-only MCP transport, durable shadow recovery, bounded official-feed ingestion, and the
2026–2028 calendar have local test coverage, not authenticated production qualification.
See [operations](docs/OPERATIONS.md), [source coverage](docs/CATALYST_INGESTION.md), and
[development log](docs/DEVLOG.md) for evidence and remaining work.
