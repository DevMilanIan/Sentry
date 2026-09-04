# Security model

## Credential boundary

Robinhood authorization material belongs in an OS-protected user store or an ignored,
permission-restricted credential directory. It is injected only into the MCP transport layer. It
must never enter prompts, source control, logs, database exports, fixtures, or screenshots.

The reasoning provider receives curated immutable packets and has no shell, filesystem,
configuration, credential, or broker tools. External content is untrusted data; prompt-like text
inside news or filings is never executed.

The MCP SDK token bridge stores validated token/client records only through
`ProtectedCredentialStore`; it has no plaintext fallback. Windows installations declare the
`pywin32` runtime dependency for DPAPI. Records are bound to the fixed MCP endpoint and retain
token save time; naive/future timestamps, malformed records, and failed protected-store access
fail with sanitized authentication errors. Use a separate key prefix for each explicitly
selected account; changing the intended account requires fresh selection and qualification,
not copying credential files or reusing a prefix. Do not print SDK token/client objects: those
models themselves contain secret fields.

Keep the Windows credential directory outside OneDrive and the repository, under
`%USERPROFILE%\.options-sentinel\oauth`. The DPAPI store now creates a dedicated directory
with a protected NTFS ACL for the current user, SYSTEM, and Administrators, and validates
existing ACLs without silently changing them. It rejects reparse paths, hard-linked records,
and synced/workspace locations. Record replacement uses exclusive random temporary files and
atomic replacement. Native fixture tests verify both DPAPI and ACL behavior. Encryption does
not justify syncing credentials or sharing access to the signed-in account. No production
broker credentials have been created by setup.

Normal `ReadOnlyMcpSession` entry requires existing unexpired credentials. Noninteractive OAuth
callbacks refuse authorization, and 401/403 responses stop before automatic discovery or
registration. The separate interactive-provider factory is inert until an explicitly invoked
flow supplies callbacks. The separate native `app.broker.oauth_bootstrap authorize` CLI now
provides an explicit operator-triggered PKCE/loopback workflow; ordinary startup never calls it.
It requires interactive confirmation, binds only 127.0.0.1, validates state/issuer/endpoint
policy, suppresses SDK credential-bearing diagnostics, and permits no business tool calls.
See `ROBINHOOD_OAUTH_SETUP.md`. Its fixtures passed; actual authorization has not been run.
Automatic credential refresh and authenticated reconnect still require implementation and
qualification; this fail-closed transport is not evidence of unattended broker readiness.

## Network boundary

The dashboard binds to `127.0.0.1` by default. PostgreSQL is not published by Compose. A private
VPN may be configured separately; no public inbound port is required. Keep Windows Firewall and
endpoint protection enabled.

Compose requires `POSTGRES_PASSWORD`; there is no built-in production password fallback.
For Windows setup, `scripts/windows/Initialize-LocalEnvironment.ps1` generates independent
256-bit URL-safe hex credentials in `%USERPROFILE%\.options-sentinel\runtime.env`, outside
OneDrive and the repository. It protects the directory before writing, disables inherited
ACLs, and permits only the current SID, SYSTEM, and local Administrators. The file is created
with `CreateNew` and never overwrites an existing file. Repeated runs validate existing ACLs,
credential format, and the exact DEMO/OFFLINE_SIM/RESEARCH settings; invalid existing files
fail without printing their contents. An interrupted partial file requires deliberate operator
inspection and repair, not automatic replacement. LIVE authorization remains blank.

The former LocalAppData location was redirected into the Codex Windows package's LocalCache,
making it invisible to the unpackaged startup task. Explicit migration validates the known
source's ACL/profile and copies exact bytes with `CreateNew` into the fixed UserProfile path.
It neither regenerates credentials nor deletes the legacy source. Private database archives
and the preserved old Docker disk also remain outside the synced repository.

`Start-Sentinel.ps1` validates that private file, supplies it to both Compose `--env-file`
interpolation and `SENTRY_ENV_FILE`/`env_file`, and temporarily removes inherited runtime
environment overrides so database credentials cannot diverge. Direct Compose invocations must
set `SENTRY_ENV_FILE` to the same absolute path passed to `--env-file`. The Compose `.env`
fallback is for nonsynced local deployments; this OneDrive checkout must not contain secrets,
even in ignored files. Do not run `docker compose config`, inspect container environment
values, or print the private file in captured terminals: those commands can reveal credentials.
`docker compose config --quiet` validates configuration without rendering it.

Do not use the example placeholders. The local control API requires its token, but read-only
dashboard/API state is intended for the same trusted localhost user, not public hosting.
Logs recursively redact credential-named fields and URL/bearer credential patterns; arbitrary
exception text is not displayed by the controller. These filters are defense in depth, not
permission to log raw authentication responses.

The concrete read-only MCP session pins the endpoint, disables redirects and environment
proxies, bounds uncached tool discovery, and checks its fixed tool-name allowlist before a
tool call. A server's `readOnly` annotation cannot authorize unknown, order, watchlist, scan, or
other mutating names. Mutation schemas may be inspected for hypothetical intent validation
but cannot be transmitted through this session. SDK roots, sampling, elicitation, and automatic
input-required retries are not enabled. The session is not connected by default service startup;
account/schema verification and the separate shadow firewall remain mandatory.

## Trading authority

Runtime authority is the conjunction of startup environment, static profile, safety state, kill
switches, mode, data health, deterministic risk, exact approval, reconciliation, qualification
fingerprint, and a narrow execution transport. Funds, model confidence, or UI text cannot grant
authority. `BROKER_SHADOW` always uses a deny-all external-write transport.

## Threat and incident actions

- **Malicious source/prompt injection:** retain provenance, strip active content, validate grounded
  references, and reject ungrounded output.
- **Duplicate/unknown submission:** persist before send, never blind-retry, reconcile by broker ID
  and fingerprint.
- **Stale data/API outage/model crash:** disable new entries; retain deterministic monitoring and
  reconciliation where trustworthy.
- **Unexpected broker order/position/deposit in shadow:** halt qualification, preserve evidence,
  reconcile, and require ownership resolution.
- **Database corruption/unwritable state:** halt all writes and restore from a verified backup.
- **Credential compromise:** stop services, revoke Robinhood authorization, rotate local tokens,
  inspect logs, and repeat qualification.
- **Any shadow write transmitted:** critical incident; halt, reconcile, fix the bypass, and restart
  the full qualification window.

Downloaded setup artifacts under ignored `var/tools` are not application credentials.
The EDB installer has been signature-checked but could not run without elevation. No
unsigned PostgreSQL executable, OS elevation workaround, or unapproved broker flow was run.
