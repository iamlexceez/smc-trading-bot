# Objective, Research, Execution, and Reduction Specification Scorecard

**Basis.** This scorecard is based on repository inspection, existing tests, the supplied Telegram/runtime diagnostics, and the offline smoke suite. Scores refer to implementation completeness and evidence quality, not trading profitability. A score of 95–100% was left unchanged unless a demonstrated defect was found.

| Requirement area | Current score | Evidence-backed assessment | Action |
|---|---:|---|---|
| Objective intent and aggressive semantics | 96% | Objective preferences are persisted and do not force trade frequency, diversification, or bad setups. | Left intact. |
| Objective persistence and versioning | 92% | Confirmed templates, versions, reset-separated sessions, immutable history, and terminal reviews exist. | Extended configured reset auto-resume to create a fresh session from the unchanged template. |
| Growth-objective phases | 96% | Adaptive geometric phases, phase summaries, completion/failure, and learning handoff exist. | Left intact. |
| Capital reduction separation | 96% | Dedicated tables and engine remain independent of objective phases; reduction sessions contain no phase or objective link. | Hardened temporary blockage and notifications. |
| Capital-reduction target completion | 96% | Completion is based on fresh broker equity within the configured tolerance. | Left target authority intact. |
| Closest broker-valid reduction action | 91% | Candidate search validates broker metadata, price, volume, margin, stops, and tolerance. | Added absolute post-action distance ranking and nearest-unreachable candidate diagnostics. |
| Reduction overshoot prevention | 95% | Overshoot envelope and proximity taper prevent unapproved overshoot. | Left safety constraints intact. |
| Causal structure and replay timing | 95% | Availability indices are attached to structural events, zones, liquidity, and confirmations; replay regression exists. | Left intact after prior causal upgrade. |
| Higher-timeframe and price-structure thesis | 90% | Existing SMC structure, HTF bias, BOS/CHOCH, order blocks, FVGs, liquidity, and thesis persistence are integrated. | No duplicate detector created. |
| Liquidity context | 88% | Pools and sweep context exist, but synthetic-index behaviour still needs broker-sample validation. | Evidence remains descriptive until DEMO samples accumulate. |
| Premium/discount context | 95% | Dealing-range context is calculated and used as supporting evidence rather than an absolute gate. | Left intact. |
| ATR, volatility, and regime context | 93% | ATR, ADX, RSI, momentum, compression/expansion, efficiency, persistence, and regime transitions are available. | Left intact. |
| Execution quality and spread | 84% | Broker price freshness, tick economics, stops, freeze levels, margin, and preflight checks are enforced. | Spread/expected-range influence remains policy-dependent and must not revive a previously removed hard gate. |
| Session context | 82% | Existing session concepts are available, but Forex session assumptions are not imposed on Synthetic Indices. | Left instrument-specific and evidence-governed. |
| News/event context | 62% | No connected event feed was evidenced in the repository/runtime. | Must remain explicitly unavailable rather than fabricated. |
| Indicator discipline | 93% | Indicators support structure and regime analysis; they do not independently manufacture entries. | Left intact. |
| Candidate scoring | 90% | Setup quality, strategy fit, evidence, regime, volatility, portfolio conflict, and execution geometry are represented. | Learning may adjust policy only through chronological evidence. |
| Cross-instrument opportunity ranking | 96% | All objective-allowed broker-valid instruments are analyzed and only the strongest thesis proceeds. | Left intact. |
| Multi-instrument and layering discipline | 91% | Layering requires thesis validity, confirmation, and evidence; artificial diversification is not used. | Correlation evidence remains an improvement area. |
| Correlation and portfolio exposure | 80% | Portfolio conflict and concentration are present, but macro-factor exposure is approximate for Synthetic Indices. | No unsupported correlation model added. |
| Structural TP and actual RR | 95% | Structural target source, risk distance, reward distance, actual RR, and configurable RR filtering are separated. | Left intact. |
| Invalidation-based SL | 95% | Structural invalidation, broker-normalized stops, and no-stop-widening protections exist. | Left intact. |
| Broker-based position sizing | 95% | Contract size, tick size/value, price distance, min/max/step, margin, and adaptive minimum-lot risk are broker-derived. | Left intact. |
| Growth efficiency and protection | 92% | Protection score, adaptive management, margin pressure, and capital state controls are integrated. | Left intact. |
| Adaptive risk | 88% | Equity health, drawdown, evidence, volatility, and broker minimum volume influence policy inputs; adequate sample depth remains necessary. | No fixed aggressive risk doctrine added. |
| Independent position management | 96% | Position management continues during scanning pauses, margin pressure, objective terminal states, and capital reduction. | Left intact. |
| Learning governance and rollback | 93% | Champion/challenger chronological windows, forward DEMO, evidence confidence, rollback, and journals exist. | Left intact. |
| Off-market learning provenance | 84% | Historical, broker-confirmed, and model-derived evidence are separated in the architecture; external knowledge is not claimed without a connected source. | No external source was fabricated. |
| Strategy-family selection | 96% | Declarative registry, regime applicability, context evidence, transitions, alternatives, and current strategy selection are implemented. | Left intact. |
| Learning report and Telegram transparency | 91% | `/learning`, `/learned`, `/opportunities`, `/engine`, reduction status, and activity reports expose evidence and uncertainty. | Reduction wording was hardened in this upgrade. |
| Technical integrity | 96% | Duplicate prevention, broker authority, stale-data checks, invalid-order protection, crash handling, and LIVE restrictions remain. | Left intact. |
| Testing completeness | 86% | Deterministic unit/smoke coverage is broad, including causality, strategy evidence, reduction, sizing, objective phases, and Telegram rendering. | Connected MT5 integration and real broker execution still require VPS verification. |

## Confirmed changes in the current upgrade

The current working upgrade changes only demonstrated gaps. The reduction planner now ranks eligible actions by absolute distance between expected post-action equity and the configured target, while reporting the closest executable candidate when no action fits the tolerance. Temporary broker/action unavailability keeps the reduction session `ACTIVE` for scheduled retry instead of terminalizing it. Reduction reports use continuous-session states and do not report growth phases. When `/demo_auto_resume on` is explicitly enabled, a fresh broker-verified reset can create the next session from the unchanged saved objective template.

## Remaining runtime limitations

The offline sandbox cannot import MetaTrader5 or verify live broker execution. Consequently, no claim is made here that a real Deriv DEMO order was submitted, closed, or managed by this revision. VPS verification remains required for broker symbol metadata, stop/freeze constraints, actual reduction distance, and Telegram delivery.
