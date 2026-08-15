# Objective-Wide Trading Policy Audit

## Scope

This audit compares the attached objective-wide policy against the existing SMC Trading Bot implementation. The audit is based on source inspection of the scanner, broker-universe handoff, strategy registry and evidence selection, opportunity ranking, risk sizing, position management, objective lifecycle, and optimizer governance.

## Components already substantially satisfied

| Requirement | Existing implementation | Assessment |
|---|---|---|
| Broker-authoritative universe | `DerivMarketUniverse` and fresh MT5 metadata gate; fail-closed when no usable instruments exist | Preserve |
| Analyze objective-allowed instruments | `_apply_operational_objective()` sets `analysis_symbols` to every explicit objective symbol or every current broker-usable symbol for dynamic objectives | Preserve |
| Execute only the strongest current candidate | `_scan_and_execute()` gathers all thesis-qualified candidates, calls `rank_opportunities()`, and sends only the first-ranked candidate to final execution | Preserve |
| Structural and causal analysis | Closed-candle structure, HTF structures, liquidity, displacement, POI, confirmation, regime, and causal availability metadata are already recorded | Preserve |
| Instrument/regime strategy selection | Registry-defined strategies plus stable and transition evidence are evaluated by `evaluate_strategies()` | Preserve |
| Historical/OOS/forward DEMO governance | `SelfOptimizer` uses chronological train/validation/OOS evidence and requires broker-realized champion and challenger forward-DEMO comparison before promotion | Preserve |
| No revenge trading | Research governance excludes loss streak and account P/L from immediate policy selection; policy changes are daily-governed | Preserve |
| Broker-aware adaptive sizing | `RiskManager.calculate_position_sizing()` uses current equity, free margin, actual contract/tick/volume data, and adapts to an executable broker minimum when margin permits | Preserve |
| Protection independent of new entries | Position management runs before new-exposure gates and continues when scanning is paused or blocked | Preserve |
| Stop integrity | MT5 executor validates and normalizes protective levels; no-stop-widening behavior is retained | Preserve |
| Phase boundary protection | Commit `9989417` added protection-first then broker-confirmed close behaviour before successor-phase activation | Preserve |

## Verified gaps addressed by the next implementation

1. Opportunity ranking does not receive a structured account-capacity context. It penalizes only same-symbol exposure and does not expose low-capital selectivity, free-margin pressure, protected-equity context, or objective progress in the opportunity board.
2. A scan with no thesis-qualified candidate sets a diagnostic gate but does not persist an explicit `NO_TRADE` decision with account-state, evidence, exposure, and capacity reasons.
3. Non-selected candidates receive a generic rank-gap explanation rather than a structured explanation covering evidence, uncertainty, portfolio conflict, capacity, and objective relevance.
4. The research cohort and strongest-opportunity ranking exist, but the learning system does not yet treat one-vs-two-vs-three position concentration as a first-class testable policy hypothesis.
5. The current policy schema contains `max_positions`, but the generator does not vary it and ranking does not use account capacity to decide whether additional opportunities are justified.
6. Capital-protection context already measures equity position, drawdown, margin pressure, unrealized loss pressure, concentration, and open-position count, but its account-state output is not yet integrated into cross-symbol ranking and no-trade telemetry.

## Implementation boundary

The changes will extend the existing ranking and evidence-governance paths. No second scanner, ranking engine, risk engine, or position manager will be created. Broker validation, emergency pause, duplicate prevention, stale-data checks, execution synchronization, and LIVE prohibition remain authoritative and are not policy variables.

The new selection behaviour will be descriptive and policy-driven. It will not impose an arbitrary permanent quality threshold. Low-capital selectivity, capacity, maximum simultaneous positions, concentration, and protection sensitivity will be represented as experiment inputs and measured through the existing chronological, OOS, and forward-DEMO governance path. A no-trade outcome will be recorded as a successful decision when no opportunity justifies new exposure.

## Data limitations

The sandbox has no live MT5 connection, so broker-specific price, margin, tick, and position outcomes cannot be verified here. The implementation and tests will use deterministic fixtures for policy logic; VPS validation remains required for broker execution behaviour.
