# Causal Thesis and Evidence Upgrade Design

## Design goal

Upgrade the existing DEMO research path without replacing its broker-data, setup-validation, objective, execution, management, or champion/challenger engines. The work adds **explicit availability metadata**, **multiple candidate theses per instrument**, and **richer outcome evidence** to the current single persistence and execution route.

## Integrated changes

| Change | Existing component extended | Why the current implementation is insufficient | Minimal implementation |
|---|---|---|---|
| Feature availability | `analysis.indicators`, `analysis.structure`, `analysis.supply_demand`, `analysis.confirmation`, `analysis.liquidity` | Source-candle indexes can be confused with the bar that confirmed the feature. | Add `available_index` / `available_at` to source feature dataclasses. Use the confirmation bar for swings, order blocks, supply/demand zones, FVGs, and inside-bar breakouts. Pools inherit a swing's available index. |
| Causal guard | New small `analysis.causality` helper | Replay records bar counts but cannot assert that a specific feature was available by decision time. | Add a reusable decision-time check and serialized timing evidence. Any late feature is excluded from a candidate and recorded as unavailable rather than silently used. |
| Regime behavior and transitions | `analysis.opportunity.market_context`, `analysis.profiler` | Current six-state regime is based mainly on ATR/ADX/RSI/momentum and has no transition record. | Add directional efficiency, trend persistence, range compression/expansion, and recent displacement proxies. Calculate prior closed-window regime and transition. Persist the descriptive profile fields. |
| Thesis competition | `strategy/registry`, `strategy/selection`, `scheduler` | Current flow builds one direction then chooses a strategy label. | Generate directional hypotheses from current structural context, run the existing validator for each hypothesis, identify applicable registered strategy candidates, score them with regime/evidence, preserve alternatives, and return only the best valid intra-instrument thesis to the existing cross-symbol ranker. |
| Full thesis contract | `analysis.scoring.TradeSignal`, `setup_records` | Important context is dynamically attached and is not consistently persisted or replayable. | Add optional first-class fields for regime transition, primary/alternative thesis, evidence, invalidation, expected value, and management plan. Persist JSON context on the existing setup row. |
| Evidence depth | `strategy_evidence`, `adaptive_management`, `storage.db` | Existing evidence is mostly averages and count bands. | Calculate long-term and exponentially recency-weighted expectancy, sample variance, standard error/95% interval when meaningful, MAE/MFE quantiles, target-reach probabilities, and per-context layering outcomes. Retain sample-stage labels but expose a separate evidence-quality/uncertainty basis. |
| Regime-transition evidence | `storage.db` | Strategy evidence is keyed to stable regime only. | Add a transition evidence table derived idempotently from the existing closed trades/setup records. It supplements—rather than replaces—the stable-context evidence table. |
| Opportunity ranking | `analysis.opportunity` | Existing descriptive weights do not make expected value or uncertainty explicit. | Add observed EV / downside / uncertainty components where closed evidence exists, retain existing portfolio-conflict penalty, and label zero/weak evidence as exploration. Ranking remains descriptive; final broker, sizing, portfolio, and execution gates remain untouched. |
| Accurate volume terminology | `analysis.order_flow`, Telegram output | MT5 tick-volume proxy is named `delta_intensity`. | Rename the field and report label to `tick_volume_intensity`; do not use it in execution. |
| Replay parity and tests | `backtest.engine`, smoke suite | Historical replay does not use live registry selection and contains an undefined RR reference. | Fix the undefined reference; use the same candidate-selection helper on causal slices; add deterministic leakage, timing, transition, evidence, target-distribution, layering, no-trade, and promotion tests. |

## Non-changes

The upgrade will not alter the broker-derived universe, MT5 order validation, live mode, immediate-close capital reduction, account/session/objective lifecycle, final entry integrity checks, no-stop-widening rule, one-action-at-a-time management behavior, or DEMO-only promotion rule.

## Execution semantics

A candidate becomes executable only after all of these existing steps remain true:

```text
causally available feature set
-> valid directional thesis
-> selected registered strategy and explicit evidence state
-> strongest intra-instrument thesis
-> strongest cross-symbol opportunity
-> final fresh revalidation
-> broker protective-stop / symbol / sizing / margin / portfolio validation
-> DEMO MT5 order
```

No evidence statistic, score, or strategy label is allowed to override a broker or software-integrity rejection.
