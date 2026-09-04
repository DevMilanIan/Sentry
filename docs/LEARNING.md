# Deterministic closed-position review

The composed runtime runs a bounded closed-position review every 60 seconds. Offline
review shares the replay/execution lock and waits for journal reconciliation, so it cannot
observe an order update halfway through fill persistence. Other runtime integrations must
provide equivalent journal-writer serialization; independent database reads are not a snapshot
transaction across an in-progress multi-write journal update.

`ClosedPositionReviewWorker` reads only the current environment/namespace's durable fills and
matching exact broker orders. Complete long-option flat-to-flat cycles produce immutable
`trade_outcomes` records with entry/exit fill IDs, contract multiplier, gross entry cost,
gross proceeds/P&L/return, holding time, and an evidence hash. New entry cycles have separate
stable identities. Repeated ticks, restarts, and competing writers do not duplicate a review;
PostgreSQL migration `0003_closed_trade_outcomes` adds the namespaced unique outcome index.

Incomplete scans, orphan/conflicting fills, inconsistent instrument identities, oversells,
future evidence, and journal quantity mismatches never become fabricated completed trades.
Unclosed positions have no completed review. Missing fees, net P&L, MAE/MFE, quote-path,
catalyst attribution, and theta/IV attribution remain explicitly unknown. Recorded results
are arithmetic on the supplied evidence, not evidence of strategy skill or expected returns.

Inspect recent namespace-bound records at `GET /api/trade-outcomes?limit=50` on the trusted
localhost API (maximum 200 per request). Archives and simulation namespaces remain distinct.
The controlled `LearningReviewer` proposal builder remains separate: at least 30 observations,
explicit evidence and before/after metrics, benefit/downside/confidence, and no apply method.
This work does not automatically generate strategy changes, evaluate rejected candidates'
later paths, infer unavailable attribution, or modify any configuration or trading gate.
