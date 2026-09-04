# Native Robinhood OAuth bootstrap

Implemented, fixture-tested; **no real Robinhood authorization has been run**.
This is a separate interactive setup command, not startup automation. It does
not complete BROKER_SHADOW composition, verify account identity, qualify a
session, request funding, or enable trading.

## When the operator is ready

First finish credential-free offline parity and baseline fault verification.
Use the same intended Agentic account for later shadow qualification and Live.
Do not intentionally fund it for this step. Read the broker's consent screen:
the granted token may have broader permissions than this command uses. The
read-only restriction here is an application operation boundary, **not a claim
that Robinhood provides a read-only OAuth scope**.

The private setup must already have created `%USERPROFILE%\.options-sentinel`.
In a native Windows terminal, from the repository root, run:

```powershell
.\.venv\Scripts\python.exe -m app.broker.oauth_bootstrap authorize
```

The command requires an interactive terminal and the exact typed confirmation
`AUTHORIZE`. Complete the browser login/consent yourself; never paste passwords,
codes, tokens or account credentials into the terminal, chat or repository.
The default deadline is five minutes (maximum ten via `--timeout-seconds 600`).
Cancelling or timing out before the authenticated handshake completes leaves
existing stored credentials unchanged. Final protected file writes are atomic
per record, not a multi-record transaction; a storage failure after a successful
handshake reports failure and requires checking/rerunning setup, never assuming
success from the browser callback alone.

Success prints only:

```json
{"account_qualified": false, "authorization_saved": true, "trading_enabled": false}
```

This proves an authenticated MCP protocol handshake and protected token storage,
not successful account reads, accepted account type, market-data coverage,
approved schemas, qualification, or execution authority. No account or order
tool is called. The existing runtime remains unchanged and fail-closed.

## Protection and protocol boundaries

- The pinned official MCP SDK 2.1.1 performs resource/authorization-server
  discovery, dynamic native client registration, S256 PKCE, resource audience
  binding, state validation, and advertised issuer-response validation.
- Bootstrap requires actual protected-resource and authorization-server
  metadata. All contacted endpoints must be HTTPS under `robinhood.com` on
  port 443. HTTP redirects, environment proxies, guessed `/authorize`,
  `/register` and `/token` fallback endpoints, and automatic scope escalation
  are blocked. A different legitimate identity-provider host requires a
  separately reviewed trust-policy change; do not disable the check.
- If Robinhood does not advertise S256 or support discovered dynamic native
  registration, setup stops. No application/client IDs or broker schemas are
  invented. A broker-required pre-registration or alternative mechanism is a
  genuine integration prerequisite, not evidence that authentication passed.
- The browser opens only a credential-free `http://127.0.0.1:<port>/authorize`
  URL. Its one-use response redirects to the in-memory authorization URL.
  State, verifier, tokens and callback codes are never command-line arguments,
  files in the repository, or diagnostic output. The listener binds IPv4
  loopback only, validates Host/state, limits headers/connections, applies
  per-request deadlines and closes on completion/cancellation. The SDK checks
  callback issuer semantics too.
- Only `server/discover`, legacy `initialize`, and its initialization
  notification may be sent as MCP methods. Protocol GET and session-close
  DELETE are allowed; business tools are not. Network responses are size-bound
  and requests have timeouts. No sampling/elicitation callbacks are enabled.
- Records are staged in memory until the authenticated handshake succeeds.
  The existing `ProtectedOAuthTokenStorage` encrypts persisted material with
  user-bound DPAPI in `%USERPROFILE%\.options-sentinel\oauth`, outside OneDrive
  and packaged LocalAppData redirection. The directory and files require the
  protected current-user/SYSTEM/Administrators NTFS policy. There is no
  plaintext or non-Windows fallback.
- Running the same command again performs interactive authorization against
  rediscovered endpoints and the saved client/loopback registration. It does
  not guess a refresh endpoint. An occupied callback port or changed client
  registration/issuer stops setup rather than replacing existing authority.
  Stored expiration deducts time spent finishing the handshake.
- Secret-bearing SDK/network diagnostics are suppressed for this dedicated
  CLI flow. Errors are deliberately static; do not rerun with HTTP wire logging
  or paste raw exception bodies to diagnose a broker response.

## Verification and remaining work

`tests/unit/test_oauth_bootstrap.py` drives the **real installed SDK** against
an in-memory HTTP transport and real local loopback sockets, with synthetic
credentials only. It covers discovery/registration/PKCE/token exchange and
legacy handshake, unsupported servers, failure preservation, callback/state
handling, bounded timeout, and business-operation denial. These are fixture
results, not proof that the public Robinhood server accepts this client today.

After an actual user authorization, remaining integration work must discover
and validate current broker capabilities/account/market schemas through the
separate read-only facade, identify the intended account without logging private
identifiers, and compose BROKER_SHADOW with its independent write firewall.
The five regular authenticated qualification sessions, funding approval and
Live activation remain separate required gates.

## Primary references checked

- [Robinhood Agentic Trading overview](https://robinhood.com/us/en/support/articles/agentic-trading-overview/)
- [Robinhood trading with your agent](https://robinhood.com/us/en/support/articles/trading-with-your-agent/)
- [MCP authorization specification](https://modelcontextprotocol.io/specification/2025-11-25/basic/authorization)
- [Official Python SDK](https://github.com/modelcontextprotocol/python-sdk)

The implementation also checks the installed 2.1.1 SDK's `mcp/client/auth/oauth2.py`,
`auth/utils.py`, `shared/auth.py`, `client/client.py`, and Streamable HTTP
transport code. Version-specific behavior is covered by the fixture test rather
than inferred from examples targeting another SDK release.
