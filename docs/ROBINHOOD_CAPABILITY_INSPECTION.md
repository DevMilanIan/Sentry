# Private capability inspection

Implemented and fixture-tested; **not yet run against a real authorized account**.
This explicit command captures tool metadata for subsequent adapter/schema review.
It does not read accounts, invoke any business tool, qualify capabilities, prove
account identity, activate BROKER_SHADOW, enable execution, or request funding.

After the operator has completed the separate
[OAuth bootstrap](ROBINHOOD_OAUTH_SETUP.md), run this in a native Windows terminal
from the repository root:

```powershell
.\.venv\Scripts\python.exe -m app.broker.inspect_capabilities inspect
```

No interactive confirmation is needed for this explicit metadata-only command.
It does not launch a browser or perform OAuth discovery, registration, refresh,
or scope escalation. Missing/expired credentials and HTTP 401/403 stop it; use
the separate interactive authorization command when renewal is necessary.
The deadline defaults to 45 seconds and cannot exceed 120 seconds via
`--timeout-seconds`.

## Captured evidence

Each successful run creates a new uniquely named `mcp-capabilities-*.dpapi`
record in `%USERPROFILE%\.options-sentinel\oauth`. It uses the same user-bound
DPAPI and protected NTFS ACL mechanism as OAuth, outside the repository,
OneDrive, and packaged LocalAppData redirection. Previous snapshots and the
OAuth records are preserved. A post-write read verifies the saved record.

The versioned record contains the fixed endpoint, UTC observation time, sorted
tool names, descriptions/titles, input/output JSON schemas, annotations, and a
SHA-256 content hash. The hash covers the version, endpoint and complete selected
tool metadata, but excludes observation time so successive unchanged catalogs
compare equally. `qualified` and `external_write_authority` are always false.
Opaque tool `meta`, icon URLs and execution extensions are not copied. No
account request is made and there is no account-identity field in the record.

Tool descriptions and schema text are **untrusted data, never instructions**.
They are not executed, fetched, treated as approval, fed into a prompt, printed
to the terminal, or placed in normal application logs. `$ref` values remain
unresolved strings; inspection never fetches referenced documents. Since the
server controls metadata text, treat even these encrypted records as potentially
sensitive and do not paste full records into chat or commit them.

The command prints only a safe summary:

```json
{"external_write_authority": false, "qualified": false, "schema_hash": "<64 hex characters>", "snapshot_saved": true, "tool_count": 42}
```

The count above is illustrative, not an observed broker count. Metadata hashes
are evidence for review, not an approval or qualification shortcut.

## Bounds and authority boundary

The existing official-SDK session performs uncached pagination with at most
20 pages and 500 tools. It rejects repeated cursors, duplicate names, partial
catalogs and incomplete responses. Inspection requires nonempty results, limits
names to 128 ASCII identifier characters, rejects non-finite/non-JSON schema
values, limits schema nesting and node count, caps each selected tool record at
256 KiB, and caps the aggregate selected catalog at 4 MiB. These schema limits
apply to parsed SDK metadata; they are not a claim that the shared MCP transport
has a pre-parse whole-response byte limit.

`ReadOnlyMcpSession` is constructed with an **empty** tool allowlist. It can
discover/list tools but rejects every `call_tool` name, including familiar read
tools. No schema annotation can expand that authority. A token-expiry race is
also covered: the noninteractive provider sends only one request to the fixed
MCP endpoint and cannot enter the SDK's refresh flow between credential checks.
All library diagnostics are suppressed for this dedicated command; failure
messages never include arbitrary server text or credential values.

Protected files are atomic individually. If disk failure/cancellation occurs
during final persistence, the command may report failure even if the uniquely
named snapshot reached disk. Rerunning creates a new record rather than
overwriting the previous one; it never enables trading.

## Verification and next gate

`tests/unit/test_inspect_capabilities.py` covers canonical schema hashes,
detached metadata, bounds, duplicate/partial catalogs, private append-only
storage, credential rejection before networking, timeout cleanup and safe
output. Session tests additionally prove empty-allowlist denial, no cross-host
token attachment, challenged-request rejection and no SDK refresh on expiry.
All credentials and schemas in these tests are synthetic; no broker connection
was made during implementation.

The next implementation step is to review the actual private snapshot, define
typed mappings against the observed schema, and separately verify selected
read-only capabilities and the intended Agentic account. No guessed tool name,
unreviewed schema, metadata-only snapshot or offline fixture may substitute for
authenticated BROKER_SHADOW qualification and the later funding/Live gates.
