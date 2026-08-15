# Runtime Audit Findings (working document)

## Observed state from user attachment

The bot reported MT5 connected and DEMO, 10 scan cycles complete, 0 symbols attempted/analyzed, 1,040 successful candle requests, M5-only timeframe activity, analysis and execution not started, learning waiting with zero observations, balance USD 17.94, equity USD 88.23, free margin USD 5.90, and 26 open positions. Broker discovery reported 799 symbols, 107 targets, 92 usable, and 15 invalid. Trading was shown as halted.

## Verified code findings

1. `scan_and_execute()` starts the market-scanner component and increments `scan_cycles_completed` after `_scan_and_execute()` returns, even when `_scan_and_execute()` exits early because the account state is blocking, auto-trade is paused, the broker universe is empty, or the objective scope is empty. This makes a blocked/no-work scan look like a completed healthy scan.

2. The zero `symbols_attempted` value is consistent with early returns before the symbol loop. The early account-state block is reached before `refresh_research_governance()` and before the loop that increments `symbols_attempted`. The exact account state/reason must be captured in the scan result and dashboard rather than inferred from the cycle count.

3. The 1,040 candle requests are not proof that 1,040 instruments were analyzed. The independent position manager calls `fetch_candles(position.symbol, "M5", 200)` for each open position on every management pass. The current telemetry aggregates these requests with analysis candles, so the dashboard conflates position-management refreshes with scan analysis.

4. Normal analysis does contain a multi-timeframe path. It fetches the configured primary timeframe (`settings.timeframes[0]`, default `M15`), up to two configured higher timeframes (`settings.htf_timeframes[:2]`, defaults `H1` and `H4`), and an M5 lower-timeframe confirmation when the primary is above M5. The observed M5-only dashboard is therefore likely aggregate position-management activity, a runtime configuration override, or a scan path that never reached analysis—not proof that the analysis architecture lacks MTF support.

5. The analysis component is only started inside the per-symbol loop. If the scanner returns before that loop, its `NOT_STARTED` state is technically accurate but lacks a reason. The execution component is only started when a ranked candidate reaches `execute_signal()`, so no candidate naturally leaves execution `NOT_STARTED`.

6. Position management is independently scheduled every 15 seconds and is called before new-entry blocking in `_scan_and_execute()`. It is also scheduled as its own APScheduler job. Existing architecture therefore preserves management when new entries are halted, but the dashboard needs to distinguish `NEW_ENTRIES` from `POSITION_MANAGEMENT` instead of using a single `TRADING: HALTED` summary.

7. The current normal SL-management path evaluates every broker-open position, but its current telemetry only counts checks and successful modifications. A no-action reason is not consistently persisted or surfaced for every position, making 1 modification across 1,066 checks difficult to interpret. The broker-confirmed SL alert fix is already present in commit `e1c84b4`.

8. The current phase-aware management/Phase 0 implementation is in commit `701d8f0`. It overlays current phase management policy on every active position, adds persisted Phase 0 recovery, and preserves independent position management. This must remain intact during the runtime-audit repair.

9. The existing opportunity ranker and explicit no-trade decisions are already present. The repair should extend telemetry and diagnostics rather than create a second ranking engine or force a trade when the account state blocks new exposure.

10. The account state must be evaluated at portfolio level. With 26 open positions and USD 5.90 free margin, new exposure should remain blocked when broker-authoritative capacity says so, while position management, account monitoring, and learning remain operational.

## Implemented repairs in this audit

- Added scan-cycle IDs and a durable scan disposition with discovered, targeted, eligible, attempted, analyzed, rejected, and failed symbol counts.
- Blocked or empty scans now produce explicit `ACCOUNT_BLOCKED`, `PAUSED`, `NO_ELIGIBLE_SYMBOLS`, or `OBJECTIVE_UNIVERSE_EMPTY` state and do not present as a healthy scan. A zero-work scan is marked degraded/blocked in the scanner component and emits a deduplicated alert when production notifications are available.
- Every candle request now records request ID, symbol, timeframe, purpose, scan-cycle ID, and outcome. Analysis, execution revalidation, and position-management candle totals are separate.
- Analysis lifecycle closes as `COMPLETED` after the symbol loop, and blocked/no-work paths expose a precise reason. Each symbol gets a persisted `analysis_outcome` execution event with a run ID, scan-cycle ID, requested timeframes, outcome, and reason.
- `observations` now increments only after broker-confirmed closed-trade reconciliation. Active positions increment `live_observations` and do not count as completed trades.
- Position management now records every no-action/skip reason, separate SL and TP modification counters, positions requiring action, and unprotected profitable positions. It remains callable regardless of new-entry blocks.
- Heartbeat and `/engine` now separate new-entry state from position-management state and show scan work accounting, candle purpose totals, subsystem reasons, portfolio risk/unrealized/protected-profit metrics, live versus completed observations, and management reasons.
- Read-only health alerts cover critical free margin, configured excessive position count, profitable positions without broker-confirmed profit protection, and candle activity without symbol analysis.
- Phase-boundary handling no longer closes an unprotected profitable position merely because protection could not be confirmed; it records the boundary as pending. Existing losing-position fallback and broker-confirmed protection are preserved.

## Validation so far

`python3 -m compileall -q .`, `python3 tests/smoke_upgrade.py`, and `git diff --check` pass after the current changes. The deterministic suite reports `PASS: upgrade smoke tests`. MetaTrader5 is not installed in the sandbox, so live broker execution remains unverified.
