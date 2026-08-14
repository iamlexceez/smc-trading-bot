# Causal and Evidence Architecture Audit Notes

**Scope:** DEMO-only research path; no LIVE execution changes proposed.

## Confirmed working components to preserve

| Component | Current implementation | Audit finding |
|---|---|---|
| Broker data and universe | `data/provider.py`, `data/universe.py`, MT5 executor | Broker-authoritative and fail-closed; preserve unchanged. |
| Broad scan / selective execution | `scheduler.py`, `analysis/opportunity.py` | Broad broker-valid analysis and strongest-candidate execution already exist; preserve the single final broker/portfolio/execution path. |
| Setup geometry | `strategy/setup_validator.py` | Separates target selection, RR calculation, and RR observation; uses structural or policy-selected stops/targets. |
| Position protection | `execution/manager.py` | Position management is independent of entry scans, emits one action at a time, and never widens a stop. |
| Policy governance | `analysis/optimizer.py`, `analysis/research_governance.py` | Chronological 60/20/20 train/validation/OOS split, forward-DEMO challenger stage, and DEMO-only promotion are already implemented. |
| Existing strategy evidence upgrade | `strategy/registry.py`, `strategy/selection.py`, `storage/db.py` | Registry selection and strategy × symbol × regime × timeframe persisted evidence exist. The current system still selects among strategies after one generic setup construction rather than competing distinct theses. |
| Objective/session integrity | `analysis/objectives.py`, `analysis/objective_phases.py`, `analysis/capital_state.py`, `storage/db.py` | Broker-verified objective lifecycle and reset-separated DEMO sessions exist; no changes should alter them. |

## Causality observations

| Area | Existing behavior | Finding | Upgrade priority |
|---|---|---|---|
| Replay visibility | `backtest/engine.py` slices LTF data through the decision bar and HTF data by timestamp; management checks stop before target within a candle | Sound foundation, but replay audit only records bar counts, not individual feature availability. | High |
| Swing points | `analysis/indicators.py` requires right-side confirmation and its doc says the swing is known at `i + lookback`; `SwingPoint` stores only source index/time | A confirmed-at field is missing. Consumers can mistake source-bar time for availability time. | High |
| Order blocks | `analysis/structure.py` detects an order block from a base candle plus the following impulse but stores only base index | The order block is not known until the impulse closes; explicit available-at timing is missing. | High |
| FVGs | Active FVG logic uses first and third candles and can know the gap only at third-candle close, but stores a single index without available-at metadata | Need explicit availability timing and regression coverage; no evidence that the active loop leaks a future bar at the final slice, but metadata must prove it. | High |
| Supply/demand zones | Zones are created from a base plus confirming impulse and freshness scans subsequent visible bars; only base index is stored | Need creation/availability index and timestamp separate from base index. | High |
| Inside-bar confirmation | `analysis/confirmation.py` uses `i+1` to confirm a breakout but only stores the confirming candle index | It is causal if evaluated after the breakout closes, but needs explicit availability metadata and replay leakage tests. | High |
| Management replay | Uses current-bar stop-first target ordering, then closed-bar structure management | Preserve intact; add feature-availability assertions around post-close management decisions. | Medium |

## Evidence and intelligence observations

| Requirement | Existing coverage | Gap to close |
|---|---|---|
| Strategy × instrument × regime × timeframe evidence | Persistent `strategy_evidence` with expectancy, PF, average MAE/MFE, drawdown, samples/confidence | Add distribution statistics, recency/current-versus-long-term evidence, uncertainty, regime transition, and layering economics. |
| Confidence | Requested sample bands are implemented | Sample count is not sufficient alone; add variance/CI and diversity/recency descriptors without inventing certainty. |
| Regime | ATR ratio, ADX, RSI, momentum classify six states | Persist previous/current regime and add behavioral metrics: efficiency, range/trend persistence, displacement and breakout behavior. |
| Instrument profile | ATR, range, persistence and simple OB/FVG outcome fields | Add durable behavioral and setup statistics; keep instrument-specific. |
| Opportunity ranking | Setup quality, selected strategy fit, regime indicators, evidence, profile, and portfolio conflict | Current coefficients are descriptive fixed weights; augment with EV/uncertainty/recency/execution feasibility components and expose evidence basis. |
| Thesis competition | Named registry chooses a strategy from applicable labels after one `SetupValidator.observe` result | Generate and evaluate multiple candidate theses from existing observations before final validation; do not duplicate detectors or final broker checks. |
| MAE/MFE learning | Averages are persisted and management summary exists | Add quantile distributions and target-reach probabilities, used only as experimental evidence. |
| Layering | Existing policy layer plans, basket sizing, fresh revalidation, and base integrity safeguards | Add per-context incremental layer outcomes; do not claim a layer is beneficial before evidence. |

## Concrete defect found during audit

`backtest/engine.py` references `required_rr` in the quality scoring call without assigning it in `BacktestEngine.run`. This can fail when a replay reaches a structurally valid candidate. It should use the same configured-observation semantics as the live scanner without reintroducing a global RR filter.

## Constraints for implementation

1. Keep DEMO-only policy experimentation; never activate or change LIVE behavior.
2. Keep broker data, validation, sizing, portfolio gates, duplicate prevention, reset/session lifecycle, emergency control, and no-stop-widening safeguards.
3. Add metadata and adapters to existing data structures rather than parallel analytical engines.
4. Treat absent or weak evidence as uncertain; it must not be promoted as positive evidence.
5. Tests must use deterministic local fixtures and must not require MT5, Telegram, or trade submission.

## Dependency flow confirmed

```text
Fresh MT5 broker candles
  -> scheduler analysis per broker-validated symbol
  -> structure / supply-demand / liquidity / displacement / regime / profile
  -> SetupValidator.observe (one directional candidate per derived direction)
  -> policy acceptance
  -> registry applicability + strategy score
  -> cross-symbol opportunity rank
  -> single strongest candidate only
  -> final revalidation + broker stops + broker sizing + portfolio checks
  -> MT5 execution
  -> independent 15-second management and broker closure reconciliation
  -> setup/trade/execution-event persistence
  -> strategy evidence + optimizer/governance/journal reporting
```

The execution route is singular and should remain singular. The minimal upgrade point for thesis competition is between existing closed-candle analysis and final `SetupValidator.observe`/policy acceptance, with all candidate theses reusing the same detectors, target/stop derivation, broker checks, and persistence route.

## Additional audit findings

1. `get_policy_trade_outcomes()` is intentionally broker-outcome-only and therefore suitable for optimizer inputs, but it currently returns feature and validation JSON without first-class selected strategy, current/previous regime, transition, or full thesis data.
2. `TradeSignal` is the existing transport contract, but regime, strategy evidence, thesis, and opportunity properties are currently attached dynamically by the scheduler. First-class optional fields would make serialisation, replay audit, reporting, and test coverage safer without changing broker execution behavior.
3. `AdaptiveProfiler._closed_candle_persistence()` correctly labels the one-bar follow-through metric as a descriptive completed-window statistic. It should remain observational; the next-bar move must not be used as an at-the-bar decision feature.
4. `OrderFlowAnalyzer` is isolated from the execution path, uses `tick_volume`, and its `delta_intensity` label should be renamed or presented as tick-volume intensity. No true order-flow, buy/sell delta, or institutional-flow conclusion is supported by the supplied data.
5. Current cross-symbol opportunity ranking uses fixed descriptive coefficients. It reports no explicit expected-value uncertainty, evidence recency, execution-feasibility estimate, or alternate-thesis comparison. It must not be changed into a second execution gate.
6. Existing research governance deliberately rotates provisional market cohorts. That exploration mechanism is distinct from strategy-level exploration and should be preserved, while selected strategy output should disclose whether it is exploratory or evidence-supported.
7. The existing backtest evaluates both directions from the same generic validator and chooses the highest quality result. It does not invoke the live registry selector and contains an undefined `required_rr` reference in the quality-scoring call. Aligning replay with the live thesis-selection adapter is necessary for decision-path comparability.

## Baseline validation

`python3 tests/smoke_upgrade.py` passed before code changes. The sandbox does not have MetaTrader5 installed; this only validates deterministic offline code and does not validate live MT5 connectivity or execution.
