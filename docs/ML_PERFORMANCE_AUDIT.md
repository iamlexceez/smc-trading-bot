# ML and Research Performance Audit

## Executive conclusion

The system currently contains an **evidence-governed deterministic policy-search engine**, not a trained predictive machine-learning model. It generates explicit policy challengers, evaluates them on chronological historical outcomes, isolates forward-DEMO experiments, and keeps LIVE promotion manual-only. Therefore, conventional ML metrics such as accuracy, ROC-AUC, calibration, precision, recall, or Brier score are not applicable to the current implementation.

No broker or completed-trade database was available in the sandbox. The repository-local `smc_bot.db` was zero bytes, MetaTrader5 was not installed, and the offline suite explicitly reported that broker data was unavailable. Consequently, no claim about live or forward-DEMO profitability, win rate, expectancy, drawdown, model accuracy, or strategy edge is made.

## Measured validation

| Check | Result | Interpretation |
|---|---|---|
| Python compilation of modified modules | PASS | Syntax and import-level compilation succeeded. |
| `python3 tests/smoke_upgrade.py` | PASS | Existing broker-safety, objective, reduction, causal replay, evidence, strategy, and Telegram regressions passed. |
| Forward-DEMO provenance regression | PASS | Realized forward outcomes retain `FORWARD_DEMO` provenance and instrument/regime partitions. |
| Expert hypothesis persistence regression | PASS | Claims remain sample-labelled, idempotent, and LIVE-ineligible. |
| Broker connectivity in sandbox | UNAVAILABLE | MT5 is not installed and no broker executor is supplied. |
| Historical/forward performance sample | UNAVAILABLE | No non-empty local evidence database or supplied historical dataset was available. |
| Static lint/type check | NOT CONFIGURED | No repository lint/type configuration was identified for this audit. |

## Governance audit

| Component | Score | Evidence-based assessment |
|---|---:|---|
| Chronological split | 95% | Outcomes are ordered and split into 60% training, 20% validation, and 20% unseen OOS windows. |
| Causal replay | 95% | Closed-candle slices and explicit feature-availability metadata are covered by regressions. |
| Forward-DEMO isolation | 90% | Challenger trades are isolated by experiment and now use broker-realized R outcomes without hypothetical rescaling. |
| Champion/challenger lifecycle | 90% | Historical filtering, forward isolation, promotion, rejection, lineage, and rollback are implemented. Promotion now requires a realized champion benchmark from the same forward interval. |
| Experiment budget | 85% | Each optimization cycle records candidate limit, generated/evaluated counts, minimum samples, split size, and chronological window proportions. Cross-cycle multiple-testing correction and a global research campaign ledger remain limited. |
| Evidence transparency | 92% | Policy metrics include expectancy, profit factor, drawdown, volatility, recovery, losing streak, and descriptive risk-of-ruin. Context evidence includes recency and uncertainty fields. |
| Instrument/regime partitioning | 90% | Strategy and forward-DEMO evidence can be partitioned by symbol and regime; evidence remains insufficient until actual outcomes exist. |
| ML auditability | 88% | Versioned policy parameters, windows, predictions/decisions, and outcomes are persisted. The system is not a black-box ML model, but a formal ML model registry is not currently needed for deterministic policy search. |
| LIVE protection | 100% | LIVE cannot self-promote. Optimization exits as observational when account mode is LIVE, and expert-hypothesis promotion remains explicitly false. |

## Confirmed correction in this audit

The prior forward-promotion comparator could fall back to a hypothetical out-of-sample simulation when the champion lacked stored forward-DEMO performance. A realized challenger could therefore have been compared against a non-realized benchmark. The optimizer now defers promotion with `forward_demo_benchmark_collecting` until sufficient broker-realized champion outcomes exist for the same forward interval. It retains both challenger and champion realized partitions in the governance record.

The forward evaluator now records:

- broker-realized forward-DEMO provenance;
- overall metrics;
- instrument partitions; and
- regime partitions.

Hypothetical risk scaling remains limited to historical candidate ranking and is labelled as hypothetical. It is not presented as broker-realized performance.

## What “ML performance” currently means

The active research engine is best evaluated as a **policy-evaluation and governance system**. It does not currently learn a probability model from a feature matrix or emit a calibrated prediction. Its present output is a ranked policy candidate and a governance decision: collect evidence, no change, rejected historical, forward-DEMO started, promoted, rejected, or rolled back.

A numerical strategy-performance conclusion requires actual broker-confirmed closed trades. The minimum evidence should include chronological train/validation/OOS windows, isolated forward-DEMO outcomes, sample sizes, expectancy, profit factor, maximum drawdown, MAE/MFE, profit retention, loss clustering, instrument partitions, regime partitions, and comparison against the contemporaneous champion.

## Remaining limitations

The sandbox cannot validate MT5 connectivity, broker execution, stop/freeze constraints, real-time position management, or forward-DEMO profitability. The system does not yet implement a separate predictive ML model, and no such model should be added merely to satisfy terminology. If a predictive model is later justified, it must add versioned feature snapshots, training/validation periods, locked OOS predictions, confidence/calibration metrics, and a no-bypass policy boundary.

The current deterministic candidate generator has a bounded per-cycle candidate batch, but research-wide multiplicity correction, a global campaign budget, and formal complexity-penalty accounting remain future improvements. They are not represented as completed capabilities.
