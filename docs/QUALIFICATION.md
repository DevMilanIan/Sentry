# Qualification record

## Status

Not started. Core credential-free implementation and safety verification are in progress.
Robinhood authentication must not be requested until offline parity and baseline fault tests pass.
Funding must not be requested until five regular broker-shadow sessions pass.

## Required evidence per session

- authenticated masked account fingerprint and capability snapshot;
- real observed account state, separate shadow snapshot, and anomaly checks;
- candidate packet/source/market/model/prompt/policy hashes;
- deterministic selector, hard-risk, exact approval, safe review, OrderIntent, and exact
  BrokerCommandIntent records;
- firewall denial and proof no write transport was called;
- reproducible shadow lifecycle/fill evidence and rejected-candidate outcomes;
- LIVE_CONSERVATIVE counterfactual and divergence reason;
- MCP latency/reconnect/schema-drift, health, restart, and incident summary.

Machine-readable reports will be written beneath the isolated `var/demo/broker_shadow/reports/`
directory and remain excluded from Git.
