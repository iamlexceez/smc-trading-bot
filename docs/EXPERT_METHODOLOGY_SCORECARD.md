# Expert Methodology Hypothesis Scorecard

## Scope and provenance

The supplied O1C/Chuks SMC/ICT material is recorded as **user-supplied expert prior knowledge**. It is not broker-confirmed evidence, historical evidence, or a permanent trading policy. The bot must test claims through the existing causal replay/backtesting and forward-DEMO evidence paths.

| Hypothesis family | Classification | Current decision | Current evidence | Current plan |
|---|---|---|---|---|
| Liquidity sweep → BOS/CHOCH → POI → continuation | HYPOTHESIS | INCONCLUSIVE | UNKNOWN; no seeded test result | Compare the full sequence with matched structural setups. |
| Two-break versus single-break structure | HYPOTHESIS | INCONCLUSIVE | UNKNOWN | Partition replay and DEMO outcomes by break count. |
| Breaker block versus ordinary order block | HYPOTHESIS | INCONCLUSIVE | UNKNOWN | Keep POI types separate in evidence. |
| Liquidity-associated versus unassociated POI | HYPOTHESIS | INCONCLUSIVE | UNKNOWN | Compare matched POIs with and without validated liquidity context. |
| Higher-timeframe bias alignment | HYPOTHESIS | INCONCLUSIVE | UNKNOWN | Compare exact closed-bar HTF-aligned and non-aligned outcomes. |
| Premium/discount location | HYPOTHESIS | INCONCLUSIVE | UNKNOWN | Measure incremental value after controlling for structure. |
| 50% retracement | LEARNABLE_PARAMETER | INCONCLUSIVE | UNKNOWN | Treat retracement depth as continuous/contextual, not a fixed gate. |
| Displacement quality | HYPOTHESIS | INCONCLUSIVE | UNKNOWN | Compare follow-through, MFE, and expectancy by displacement quality. |
| Protected highs/lows | HYPOTHESIS | INCONCLUSIVE | UNKNOWN | Measure stop validity, MAE, and target reach. |
| Continuation confirmation before entry | LEARNABLE_PARAMETER | INCONCLUSIVE | UNKNOWN | Compare expectancy, frequency, entry delay, and MFE capture. |
| Structural liquidity targets | HYPOTHESIS | INCONCLUSIVE | UNKNOWN | Compare target reach and profit retention against non-structural fallback. |
| A+ setup tier | LEARNABLE_PARAMETER | INCONCLUSIVE | UNKNOWN | Test quality tiers without imposing an unvalidated score threshold. |
| Instrument-specific behaviour | HYPOTHESIS | INCONCLUSIVE | UNKNOWN | Keep results partitioned by instrument, strategy, regime, and timeframe. |
| Timeframe combinations | HYPOTHESIS | INCONCLUSIVE | UNKNOWN | Test combinations chronologically with causal availability checks. |
| Session, ATR, and candlestick context | HYPOTHESIS | INCONCLUSIVE | UNKNOWN | Measure incremental value separately from structural price action. |

## Evidence decision policy

A claim remains **INCONCLUSIVE** until it has sufficient completed observations, a historical/replay partition, forward-DEMO observations, expectancy, uncertainty bounds, and context partitions. The evaluator uses conservative sample-depth labels: `UNKNOWN` below 3 observations, `EARLY` for 3–9, `OBSERVED` for 10–19, `PROMISING` for 20–49, `VALIDATED` for 50–99, and `STRONG_EVIDENCE` at 100 or more.

A positive decision requires a non-negative ambiguity-resolving confidence interval, at least 20 total observations, and at least 10 forward-DEMO observations. A hypothesis may become eligible for a governed DEMO challenger comparison only with at least 50 total observations, at least 20 historical observations, at least 50 forward-DEMO observations, and a positive lower confidence bound. **LIVE promotion is always false in this journal and remains manual-only.**

## Data provenance labels

The journal distinguishes the source of a claim from the source of its result. `user_supplied_expert_methodology` identifies the prior package. Recorded test results require `historical_replay`, `forward_demo`, or both, and the persistence layer labels recorded results as `DEMO_RESEARCH_EVIDENCE`. No external expert statement is presented as broker-confirmed evidence.

## Current conclusion

No supplied methodology claim is currently promoted into a permanent rule. The bot continues to use its existing broker-validated causal analysis, registered strategy selection, evidence-aware ranking, champion/challenger governance, and technical execution safeguards. Testing a claim may produce `NO TRADE`; the journal never requires activity merely because a claim is under investigation.
