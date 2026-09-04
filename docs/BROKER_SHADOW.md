# Broker-shadow qualification

The intended `DEMO/BROKER_SHADOW` deployment authenticates the preferably unfunded Agentic account to exercise
real MCP discovery, reads, quote/instrument identity, reconnect behavior, and only broker-confirmed
non-executing reviews. Zero balance is defense in depth, never the permission boundary.

Before opening any MCP session, startup validates that the external write firewall is
`DENY_ALL_WRITES`. The shadow adapter has a read/review transport and no callable place/cancel/
replace transport. Every hypothetical write is serialized against the discovered schema, linked
to proposal/risk/approval/quote/real-account/shadow-account evidence, persisted as a
`BrokerCommandIntent`, denied, and then applied only to the isolated ShadowLedger.

Qualification fails for any transmitted write, missing exact arguments/evidence, account-state
conflation, unexplained real order/position/deposit, unresolved schema drift, unreconciled shadow
state, or safety-critical incident. Each DEMO_EXPLORATORY proposal also stores a
LIVE_CONSERVATIVE counterfactual decision from the frozen packet.

## Current implementation status — 2026-09-03

Adapters and write-firewall behavior have mock coverage. The default `serve` composition
does not yet open an authenticated MCP session for this backend; it leaves broker and
execution health unavailable and external writes disabled. This is not an authenticated
qualification run. Actual account-tool and order schemas must be discovered and mapped
without assuming mock argument shapes. Missing account values, unrecognized collection
wrappers, or ambiguous review results must be treated as errors, not empty/successful data.

## Read-only connection building blocks

`app/broker/mcp_session.py` implements an async-context-managed transport using the pinned
official MCP 2.1.1 SDK (`Client`, Streamable HTTP, and `httpx2`). Construction performs no
network I/O. Entering a session requires existing, unexpired protected OAuth token and client
records before constructing the HTTP client. Sessions are single-use; their resources must
close on the entering event loop and task.

The endpoint is fixed to Robinhood's configured official MCP endpoint. Tool discovery bypasses
the SDK response cache, has page/count/time bounds, rejects duplicate/cyclic/incomplete
listings, and publishes no partial catalog. Discovery can return mutation schemas for local
intent validation, but `call_tool` only admits an explicit subset of fixed read/review names.
The default subset is `get_accounts` and `get_portfolio`; server `readOnly` hints cannot grant
new authority, and contradictory unsafe annotations deny calls. Review-tool use still needs
independent schema and non-execution evidence. HTTP redirects and environment proxy settings
are disabled; no server-driven sampling, roots, elicitation, or input-required retries are
enabled.

`ProtectedOAuthTokenStorage` bridges SDK token/client models to the protected credential store.
Its record includes the endpoint and saved time so restart cannot renew a token's remaining
lifetime. Account-specific key prefixes isolate storage; they do not establish account identity.
The Windows store uses user-bound DPAPI, with `pywin32` declared only for Windows installations.
Its caller must choose a local, non-synced directory such as
`%LOCALAPPDATA%\OptionsSentinel\credentials`, outside this OneDrive workspace. No credential
directory/material is created by the transport constructor. Verify the actual Windows ACL;
`os.chmod(0o600)` alone is not proof of an owner-only NTFS ACL.

Normal service authorization never starts a browser or callback listener. Missing/expired
credentials and HTTP 401/403 raise `AuthenticationRequiredError` before OAuth discovery or
registration. Unattended refresh/reconnect is not yet implemented or qualified. A separate,
inert interactive-provider factory requires explicitly supplied metadata and callbacks for a
future user-invoked authorization flow; it is not wired into the CLI or `serve`.

These building blocks have mocked SDK-factory tests, not authenticated connectivity evidence.
Actual account selection, response schemas, review safety, reconnect/refresh, and the five
regular shadow sessions remain qualification work. No OAuth flow, funding, or live write was
performed to build or test this transport.
