# Current-data surveillance and research composition

The current-data worker and durable research queue are implemented and composed through
explicit `build_application(..., market_provider=..., market_watchlist=(...))` injection in
DEMO/BROKER_SHADOW only. OFFLINE_SIM rejects that injection before reading anything. This is
an integration seam for a verified adapter, **not a claim that a real market feed or broker
is connected**. The ordinary CLI has no authenticated adapter to inject yet; the deployed
service remains the finite offline replay.

An explicit bounded watchlist is mandatory. The worker verifies provider/capability identity,
fresh causal quote times, option-chain bounds, and monotonic observation history. It stores
each observation before its stable event, repairs interrupted event writes after restart,
and filters shared snapshots by environment/namespace. Initial observations are labeled
MARKET_BASELINE, never unexplained price anomalies. Measured changes are distinct events.
Health rechecks the original quote timestamps each controller health tick, so the scan
interval cannot extend a stale quote's life. Failures remain unhealthy until a successful scan.

The independent research queue handles one oldest pending market event at a time, using the
existing candidate/reasoning pipeline and its durable event identity. A terminal candidate
record is required before advancing the queue cursor. Restart or an ambiguous cursor write
does not silently lose an event or authorize another execution. Source event/snapshot hashes,
namespace, provider, and causal timestamps are verified before research. Proposed trades are
visible only; this composition has no authenticated broker or execution dispatcher and remains
DENY_ALL_WRITES. Real account/schema qualification is still required.

Limits are deliberate and visible: watchlist/chain size and historical recovery have hard
bounds; a pending queue larger than its provable 1,000-record window fails instead of silently
discarding older evidence. Configure a small verified universe and measure throughput before
qualification. This worker does not infer missing provider licenses, quote capabilities,
broker schemas, account identity, or session eligibility. Provider-backed public sources are
never merged into the offline replay clock.
