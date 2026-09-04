# Official-source ingestion status

## Implemented and checked locally

`OfficialSourceCollector` supports bounded RSS/Atom over credential-free HTTPS. It uses an
overall deadline as well as per-request timeouts, rejects redirects and unsupported documents,
disables environment proxies, bounds response bytes and document counts, and disables XML
entities. Source text remains untrusted data. Unknown publication time zones remain unknown.

`CatalystIngestionWorker` polls only explicitly enabled sources, after checking writable audit
storage. It records normalized source documents with URL, publication/fetch timestamps, stable
identity, stored content hash, and `LIVE_READ` provenance. It persists documents before their
environment-scoped sentinel events and repairs this gap from durable documents on subsequent
polls, even if the remote feed no longer contains an interrupted item. Recovery scans are
bounded at 100,000 rows and fail explicitly if exhausted; large deployments need an indexed
transactional outbox before exceeding that bound. Cross-process shared document insertion is
not claimed exactly-once; stable document/event identities preserve correlations.

Deduplication is deterministic. It retains conflicting revisions at one URL and corroborating
text from independent sources instead of randomly selecting a revision by generated UUID.
Unknown, future, and older-than-seven-day publication dates are retained for inspection but do
not emit current-catalyst events. Event availability is the observed fetch time, not the earlier
publication time. No ticker associations or market significance are guessed from a headline.

The worker is registered only outside `OFFLINE_SIM`. Replay workers do not poll current feeds;
candidate source facts additionally separate live-read evidence from fixture/replay evidence.
This module does not invoke a model, touch brokerage credentials, establish market-data
freshness, place orders, or advance qualification gates.

## Source coverage and limitations

The [Federal Reserve official feed](https://www.federalreserve.gov/feeds/press_all.xml) was
successfully read on September 3, 2026: 20 parsed documents had source, URL, and hash metadata.
This was a bounded public-read diagnostic, not a persistent ingestion deployment. It is the
only enabled feed in `config/sources.yaml` at this checkpoint.

The other configured agency URLs remain disabled pending current endpoint/access-policy
verification. They are candidates, not certified working adapters. SEC ingestion additionally
requires a real identifying contact in the User-Agent; the checked-in example cannot be used
for SEC requests. SEC press releases are not an EDGAR filings adapter. EDGAR submissions,
company investor-relations sources, verified entity mapping, and downstream current-market
candidate consumption remain unfinished. Do not count this feed parser as complete source
coverage or a verified continuous catalyst surveillance service.

Mocked tests cover disabled sources, malformed items, bounded trickling responses, unsafe XML,
URL/timestamp provenance, retained revisions, restart deduplication, rolled-feed crash recovery,
database failures, and explicit OFFLINE_SIM network separation. Real PostgreSQL persistence,
continuous source availability, and multi-process duplicate handling remain deployment tests.
