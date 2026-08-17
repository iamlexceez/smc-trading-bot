# Audit Closure Mandate Notes

Source: `/home/ubuntu/upload/pasted_content.txt`, user-provided attachment received in this task.

The mandate requires closing every remaining PARTIAL and UNVERIFIED item from the latest 58-section traceability audit while preserving existing architecture and safety. It explicitly prohibits broad rewrites and requires AUDIT → TRACE → VERIFY → IMPLEMENT → INTEGRATE → TEST → RE-AUDIT.

Required closure areas identified in the attachment:

1. Create a machine-readable 58-section requirement matrix with section, requirement, status, files, functions/classes, scheduler entry point, durable evidence, tests, gap, required change, and verification method.
2. Create an automated module invocation matrix proving each subsystem exists, imports, is called, is scheduled, receives live data, produces durable evidence, is consumed downstream, and has an integration test.
3. Complete external research provenance: source, author/methodology, identifier, retrieval timestamp, concepts, rules, assumptions, timeframe, entry/exit/risk rules, limitations, hypothesis ID, and version.
4. Complete explicit hypothesis metadata: independent/dependent variables, market/instrument/timeframe/regime, entry, SL, TP, risk, sample size, evaluation period, success metric, and failure metric.
5. Complete live population of individual feature and concept-combination evidence, including present/absent performance, baseline/conditional performance, incremental value, sample size, confidence, OOS, forward, drawdown, and status.
6. Complete consistent multi-timeframe context and top-down coverage where applicable: Monthly, Weekly, Daily, H4, H1, M30, M15, and M5.
7. Complete event provenance for structure/liquidity/zone events: event ID, instrument, timeframe, timestamp, source candle, type, direction, reference level, confirmation time, invalidation time, and no future-candle leakage.
8. Expand strategy metadata with concepts, required/optional/conflicting concepts, preferred/invalid combinations, timeframes, regimes, entry/exit/risk/execution requirements, evidence, and version.
9. Preserve the earned Core instrument system and complete Research → Candidate → Challenger → Core → Declining → Review → Quarantine, evidence-based replacement, demotion, correlation, small-account economics, and portfolio contribution.
10. Complete objective separation and the phase transition matrix, keeping capital reduction independent from objective phases.
11. Resolve the RR contradiction explicitly with one authoritative source covering min RR, preferred RR, strategy RR, objective RR, TP policy, research RR, and execution RR; do not silently choose.
12. Complete experimental DEMO schema and broker-realized cohort validation with experiment ID, hypothesis ID, strategy version, instrument, regime, timeframe, entry, SL, TP, risk, expected/actual result, and evaluation status.
13. Implement a strict chronological TRAIN → VALIDATE → TEST → FORWARD pipeline and tests for lookahead, future-candle/outcome leakage, train/test contamination, parameter leakage, and feature-normalization leakage.
14. Complete model governance and live drift effects through explicit governed rules only: confidence, research priority, strategy authority, and instrument status.
15. Verify broker-confirmed SL protection and realistic small-account position management at $10, $20, $50, $100, and $200.
16. Complete broker-mock scenarios: invalid volume/stops, insufficient margin, stale/missing quotes, freeze/stop levels, partial fill, rejection, reconnect, symbol disappearance, and price changes.
17. Complete communication failure isolation, scanner-health distinctions, dead-module detection, full provenance, explainability, recovery, DEMO/LIVE separation, and scheduler invocation tests.
18. Final report must list total requirements, implemented, partial, blocked, unverified, files modified/added/deleted, tests added/passed/failed, and remaining risks. Never claim 100% where runtime or broker evidence is absent.

The current implementation already includes the context/knowledge/router stack, decision records, feature-importance population, combination persistence, Core specialization/diversification, drift classification, shared Telegram/Slack control, quote-freshness validation, and 48 passing tests plus smoke. Remaining closure work must target the concrete items above rather than creating parallel architecture.
