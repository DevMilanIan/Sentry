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

## Isolated backup/restore roundtrip

`test_postgres_backup_restore.py` requires the same database URL and explicit
database-creation opt-in as the migration smoke test. It also needs `pg_dump`
and `pg_restore` clients at least as new as the PostgreSQL server major version.
They are discovered on `PATH`, or under an explicitly configured
`SENTRY_TEST_PG_BIN_DIR`. A configured test without the clients fails rather
than claiming backup verification. Database URL query options are rejected so
the SQL driver and command-line clients cannot silently connect differently.

The test creates two fresh databases named
`sentry_restore_test_<random UUID>_source` and `_target`, tagging both with its
exact ownership comment. The configured database is only the administrative
connection, never the dump, restore, migration, or deletion target. The source
receives the Alembic migration chain and one filled, synthetic offline-replay
entry in a unique namespace. After all application pools close, `pg_dump`
creates a custom-format archive inside the private pytest temporary directory,
and `pg_restore --single-transaction --exit-on-error` restores the archive into
the empty target. No operational or authenticated account data is involved.

Verification compares every audit row in `shared`, `demo`, and `live`, including
exact row IDs, append sequences, and JSON payloads, plus the Alembic revision.
A newly constructed runtime must recover the exact ledger and its order,
command, fill, and position identities; a subsequent append must advance the
restored sequence. Cleanup validates the exact created database names and
ownership comments, then uses ordinary `DROP DATABASE`, never `FORCE` or session
termination. Unverified ownership or unexpected open sessions leave evidence
for operator inspection instead of deleting it.

Credentials are passed only in the client process environment, never command
arguments. Unrelated broker credentials and ambient PostgreSQL service/options
variables are excluded, password-file fallback is disabled, and raw client
failure output is suppressed. Three local subprocess/environment cases test
these boundaries separately from the one actual database roundtrip. This is a
synthetic restore drill, not a verified restore of an operational backup, and
does not establish backup retention, encryption, or recovery-time objectives.

## Current-data and review recovery

The verification target also includes `test_trade_outcomes_postgres.py` (separate-pool
writer races, immutable review identities, and restart preservation) and
`test_live_surveillance_postgres.py` (snapshot/event gap repair, stable event IDs across
new clients, and explicit namespace filtering of shared market snapshots). All use the
existing fresh ownership-checked schema fixture and synthetic data, never authenticated
account data. The operational `Backup-Database.ps1` is a separate Windows command that
now creates and verifies a real deployed database archive without replacing production.
