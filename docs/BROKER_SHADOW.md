# Broker-shadow qualification

`DEMO/BROKER_SHADOW` authenticates the intended, preferably unfunded, Agentic account to exercise
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

