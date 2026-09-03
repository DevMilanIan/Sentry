# Options Sentinel

Options Sentinel is a local-first, event-driven options surveillance and execution platform.
It defaults to credential-free `DEMO/OFFLINE_SIM + RESEARCH`, uses deterministic risk and
execution controls, and cannot obtain real broker write authority from model output or a
dashboard request.

The system is intended for engineering and qualification of a very small experimental
account. It does not promise profitability and is not financial advice. Real trading remains
locked until account-backed broker-shadow qualification, explicit funding, and user activation
are all independently documented.

## Safe quick start

Requirements: Python 3.12+, or Docker Desktop/Compose. Ollama is optional for health checks and
reasoning; deterministic surveillance, replay, and position monitoring continue without it.

```powershell
Copy-Item .env.example .env
python -m venv .venv
.\.venv\Scripts\python -m pip install -e ".[dev]"
.\.venv\Scripts\python -m pytest
.\.venv\Scripts\python -m app.main demo-once
```

Or start PostgreSQL and the application:

```powershell
Copy-Item .env.example .env
docker compose up --build
```

The dashboard binds to <http://127.0.0.1:8000>. State-changing controls require the dashboard
token configured outside source control. See `docs/OPERATIONS.md` before running continuously.

## Safety boundary

- `DEMO` and `LIVE` are startup-only execution environments with separate persistence schemas.
- `BROKER_SHADOW` initializes a deny-all external-write firewall before any MCP session.
- Every broker action first becomes an immutable `OrderIntent` and typed
  `BrokerCommandIntent`.
- Unknown submission outcomes are reconciled and never blindly retried.
- The local language model has no broker, shell, configuration, or risk-control authority.
- Presence of funds never grants trading permission.

See `docs/LIVE_GATES.md` for the deliberately unmet Live requirements.

