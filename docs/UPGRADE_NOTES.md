# Trading Intelligence Upgrade Notes

## Scope

This incremental upgrade preserves the existing broker validation, DEMO safeguards, causal backtesting, evidence governance, position protection, and execution controls. It adds a read-only live market-state layer and makes the existing opportunity board more explicit about uncertainty and comparative selection.

## Live market state versus confirmed structure

`analysis.market_state` reads fresh `(bid, ask)` quotes through the active executor. It calculates spread, quote age, stale status, side-aware current price, current R, and broker-confirmed unrealized P/L. It does not create entries, alter orders, or replace broker checks.

The existing structural modules remain responsible for closed-candle analysis. Historical structure and backtests continue to use causal data rather than unfinished or future candles.

## Opportunity selection

The existing `analysis.opportunity.rank_opportunities` function remains the single ranking surface. It now adds an explicit uncertainty label:

| Label | Meaning |
|---|---|
| `INSUFFICIENT_EVIDENCE` | Fewer than three completed outcomes are available. |
| `HIGH` | Early evidence exists but remains shallow. |
| `MEDIUM` | Evidence is developing or conservative expectancy is not positive. |
| `LOW` | At least thirty observations exist and conservative expectancy is positive. |
| `CONFLICTED` | Existing same-symbol exposure creates a portfolio conflict. |

The read-only `/opportunities` report now explains why the leader was selected and why lower-ranked candidates were not selected. This is comparative context only; final broker, account, sizing, portfolio, and execution gates remain authoritative.

## Quality gates

The repository now contains `pyproject.toml`, a focused regression test module, and `.github/workflows/quality.yml`. CI runs compilation, the full pytest suite, the deterministic smoke test, and Ruff on the maintained upgrade surface. The smoke fixture also uses explicit NumPy time units to avoid deprecation warnings.

## Sample human-readable decision

> **WHY THIS TRADE?** Opportunity A ranked first because its setup quality and strategy fit were supported by positive completed-outcome evidence, its geometry was complete, and it had no same-symbol exposure conflict. Opportunity B ranked lower despite a larger theoretical RR because it had weaker or insufficient historical evidence and higher uncertainty. If the broker quote is stale, the account is unsafe, or the final sizing and execution checks fail, the correct result remains **NO TRADE**.

## Remaining validation limits

The sandbox cannot validate a live MetaTrader 5 terminal, broker symbol metadata, real tick freshness, margin responses, stop/freeze-level responses, partial fills, reconnects, or DEMO-to-LIVE operational promotion. Those require a controlled DEMO account and should be validated using the existing safeguards before any live deployment.
