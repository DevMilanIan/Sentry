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

The September 4, 2026 checkpoint enables four public feeds, verified using the actual
bounded collector without database writes or brokerage access. Official discovery pages are
the [Federal Reserve RSS directory](https://www.federalreserve.gov/feeds/feeds.htm),
[FTC RSS directory](https://www.ftc.gov/stay-connected/rss), and
[EIA RSS directory](https://www.eia.gov/tools/rssfeeds/).

| Enabled source | Parsed / timezone-aware publication times | Recent, non-future items within seven days | Newest publication UTC |
| --- | ---: | ---: | --- |
| Federal Reserve press releases | 20 / 20 | 0 | August 27, 2026 15:00 |
| FTC press releases | 10 / 10 | 3 | September 3, 2026 12:00 |
| EIA Today in Energy | 13 / 13 | 3 | September 4, 2026 14:00 |
| EIA new product releases | 10 / 10 | 6 | September 3, 2026 05:00 |

All parsed records had canonical URLs, fetch timestamps, and content hashes. The reads
occurred at 13:48–13:50 UTC. One EIA Today in Energy item was future-dated at inspection;
it must remain ineligible for a current event until its publication time. The two EIA feeds
are from the same agency, not independent-agency corroboration. A successful request with
old items is not proof of a fresh catalyst. Feed counts and availability will change.

EIA returned HTTP 406 with the previous `Accept` header. Including its official `text/xml`
media type produced HTTP 200 and valid RSS without changing the client identity, following
redirects, or relaxing parsing/budget checks. A regression test covers this negotiation.
FTC and EIA robots files allowed the selected feed paths for this client. FTC's observed
five-second crawl delay is below the configured 900-second polling interval; only one FTC
feed is enabled. Access policies require periodic re-review; the collector does not pretend
this point-in-time inspection guarantees future permission or availability.

Other coverage remains explicitly incomplete:

- Defense: the [current official RSS page](https://www.war.gov/news/rss/), reached from the
  former defense.gov directory, returned HTTP 403 to the public client. The feed remains
  disabled; no browser impersonation, proxy, or access-control workaround was used.
- Energy department general feed: parsed 10 timezone-aware items, but the newest was June
  10, 2020. It remains disabled. EIA is useful energy coverage, not a replacement for DOE
  policy/press-release coverage.
- Commerce: the official RSS page and robots request returned HTTP 403. BEA's news page
  advertises an alternate official feed, but the apps.bea.gov robots policy disallowed
  that feed path; it was not fetched or enabled.
- Treasury: the configured press-release feed timed out within the collector's 20-second
  deadline. TreasuryDirect's official auction/monthly-debt feeds were not fetched because
  its robots policy disallowed those paths. [OFAC retired its RSS feed in January 2025](https://ofac.treasury.gov/recent-actions/20241122);
  an old feed URL is not a working sanctions adapter.
- White House remains disabled pending current endpoint verification. SEC remains disabled
  because a real identifying contact is required; no request used the placeholder address.
  SEC press releases would not constitute an EDGAR filings adapter.

EDGAR submissions, company investor-relations sources, verified entity mapping, and complete
downstream current-market consumption remain unfinished. These public reads do not constitute
complete master-task source coverage or a verified continuous catalyst surveillance service.

Mocked tests cover disabled sources, malformed items, bounded trickling responses, unsafe XML,
URL/timestamp provenance, retained revisions, restart deduplication, rolled-feed crash recovery,
database failures, and explicit OFFLINE_SIM network separation. Real PostgreSQL persistence,
continuous source availability, and multi-process duplicate handling remain deployment tests.
