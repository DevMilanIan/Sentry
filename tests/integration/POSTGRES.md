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

These ten cases use `initialize_for_development()` with schema translation. They
do **not** run or prove the production Alembic migration chain, and do not test
Robinhood connectivity or authorize external trades.

## Isolated migration smoke test

`test_postgres_migrations.py` additionally requires
`SENTRY_TEST_ALLOW_DATABASE_CREATION=1`, and a role allowed to create databases.
It never migrates the database specified by `SENTRY_TEST_DATABASE_URL`: that
connection creates a fresh database named `sentry_migration_test_<random UUID>`,
without `IF NOT EXISTS`, and tags it with an exact ownership comment. The test
upgrades that disposable database to Alembic head, repeats the upgrade, and checks
the revision, all shared/demo/live tables, ingestion identities, and execution
identity indexes. This is fresh-database/repeated-upgrade coverage, not a proof
of migration from every historical data shape.

Cleanup removes only the database successfully created by that test, after
matching its exact name and ownership comment. It never force-disconnects
sessions. Failed ownership validation or a forcibly stopped test leaves the
database intact for explicit operator inspection. Do not enable this opt-in for
an untrusted or production-admin connection.

## Container-side verification

After provisioning the local Compose PostgreSQL service and configuring the
ignored `.env`, run:

```powershell
docker compose --profile verification run --build --rm verify
```

The explicit `verification` profile runs all eleven real PostgreSQL cases on the
internal Compose network. The container does not mount runtime state, load the
dashboard/broker environment file, publish a port, or start the trading service.
It receives only the PostgreSQL test URL and the database-creation opt-in. Its
role is the local Compose database owner, and its disposable migration database
is separate from `sentinel`. Do not print `docker compose config` because the
rendered configuration includes the database password.

Tests and test dependencies exist only in the selected `verification` image
target; the ordinary final `runtime` target does not include them. A source-only
`.dockerignore` excludes credentials, virtual environments, downloads, Git
history, and runtime evidence from both build contexts. Until this command has
actually passed, the migration and real database checks remain unverified.
