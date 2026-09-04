# Real PostgreSQL verification

The tests in `test_postgres_live.py` use an actual PostgreSQL server through
asyncpg. They explicitly skip when `SENTRY_TEST_DATABASE_URL` is absent. A
configured but inaccessible database is a test failure, not a successful or
skipped verification.

Provide `SENTRY_TEST_DATABASE_URL` through the current process environment or a
secret manager. It must be a `postgresql://` or `postgresql+asyncpg://` URL. Prefer
a dedicated test database. The role needs permission to connect and create and
drop its own schemas; it does not need permission to change roles or databases.
Do not commit or print a credential-bearing URL.

From the repository directory in PowerShell:

```powershell
.\.venv\Scripts\python.exe -m pytest tests/integration/test_postgres_live.py -q
```

Each test creates three fresh schemas named
`sentry_test_<random UUID>_shared`, `_demo`, and `_other`. Setup fails if any name
already exists. Cleanup only drops the exact schemas created by that test after
validating both the name and its test ownership comment. Generated test rows
are discarded with those schemas; they are not retained as operational audit
evidence. The normal `shared`, `demo`, and `live` schemas are never targeted.
If the process is forcibly killed, its uniquely named test schemas can remain
for an operator to inspect and explicitly remove.

The ten cases cover schema/environment/namespace isolation, equal-time append
ordering and keyset pagination, the three order-intent unique indexes, competing
independent immutable writers, transaction/probe rollback, read-only write-health
failure, and replay ledger recovery across fresh connection pools.

These tests use `initialize_for_development()` with schema translation. They do
**not** run or prove the production Alembic migration chain, and do not test
Robinhood connectivity or authorize external trades. Migration testing still
requires a separately designated disposable database and explicit migration
setup; do not run the production migration command against an arbitrary existing
database merely to enable this suite.
