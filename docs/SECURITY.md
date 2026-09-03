# Security model

## Credential boundary

Robinhood authorization material belongs in an OS-protected user store or an ignored,
permission-restricted credential directory. It is injected only into the MCP transport layer. It
must never enter prompts, source control, logs, database exports, fixtures, or screenshots.

The reasoning provider receives curated immutable packets and has no shell, filesystem,
configuration, credential, or broker tools. External content is untrusted data; prompt-like text
inside news or filings is never executed.

## Network boundary

The dashboard binds to `127.0.0.1` by default. PostgreSQL is not published by Compose. A private
VPN may be configured separately; no public inbound port is required. Keep Windows Firewall and
endpoint protection enabled.

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

