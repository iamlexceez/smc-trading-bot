# Master Trading Intelligence Rebuild Audit

## Scope

This audit compares the attached Master Trading Intelligence Rebuild Specification with the current SMC Trading Bot repository before implementation changes.

## Specification principles captured

The specification requires a layered data-to-learning architecture: market context, regime, structure, knowledge, strategy routing, evidence, meta-decision, risk, deterministic execution, position management, outcomes, and governed learning. It explicitly separates knowledge, skill, evidence, confidence, research, DEMO, and production. It requires no-trade to remain valid, chronological validation, counterfactual learning, small-account economics, explicit trade theses, explainability, persistent audit records, and failure isolation.

The specification also requires preservation of DEMO/LIVE separation, broker and contract validation, margin and volume checks, stop/freeze checks, emergency stop, manual pause, account verification, reconciliation, capital protection, persistence, and audit logs. Messaging must remain observability only and cannot affect scanning, execution, risk, or position management.

## Current repository mapping

| Specification area | Current repository components | Initial assessment |
|---|---|---|
| Data and broker | `data/provider.py`, `data/universe.py`, `executors/mt5.py` | Present; broker-authoritative universe and MT5 isolation exist |
| Context and regime | `analysis/market_state.py`, `analysis/sessions.py`, order-flow and structure modules | Present but requires contract audit against the specified vocabulary |
| Structure/liquidity | `analysis/structure.py`, `analysis/liquidity.py`, `analysis/displacement.py`, `analysis/supply_demand.py` | Present; deterministic event metadata and timing require verification |
| Knowledge | `analysis/expert_knowledge.py`, `strategy/registry.py` | Present in partial form; a unified machine-readable knowledge registry is not yet proven |
| Strategy routing | `strategy/selection.py`, `strategy/registry.py`, `analysis/opportunity.py` | Present; routing and strategy-combination authority require integration audit |
| Evidence | `analysis/evidence.py`, `analysis/decision_gates.py`, `analysis/research_governance.py`, `storage/db.py` | Present; evidence classes and counterfactual persistence exist in partial form |
| Meta-decision | `analysis/decision_gates.py`, scheduler handoff | Present; final thesis/decision record completeness requires audit |
| Risk and sizing | `risk/manager.py`, `strategy/setup_validator.py`, `executors/mt5.py`, `analysis/capital_state.py` | Present; broker economics and small-account behavior require end-to-end tests |
| Execution | `execution/manager.py`, `executors/mt5.py`, `execution/capital_reduction.py` | Present; deterministic broker validation is retained |
| Position management | `analysis/adaptive_management.py`, `analysis/capital_protection.py`, scheduler position jobs | Present and independent of scanner state |
| Outcomes and learning | `analysis/optimizer.py`, `analysis/research_governance.py`, `storage/db.py` | Present; promotion, rollback, drift, and feature/combo evidence require completion audit |
| Objective | `analysis/objectives.py`, `analysis/objective_phases.py`, scheduler and handlers | Present and must remain separate from intelligence |
| Communication | `communication/events.py`, `communication/notification_manager.py`, `communication/command_bus.py`, `communication/control_service.py`, `communication/slack_control.py`, Telegram handlers | Shared notification and command foundations present; legacy command migration remains partial |
| Persistence | `storage/db.py` | Present; notification and command audit tables added |
| Testing | `tests/*.py`, `tests/smoke_upgrade.py` | Present; broad subsystem and end-to-end coverage remains incomplete |
| Startup/recovery | `main.py`, `scheduler.py`, autonomous DEMO recovery | Present; latest resume fix is commit `25484ee` and requires VPS validation |

## Confirmed existing strengths

The repository already has explicit decision gates, broker-authoritative symbol discovery, DEMO/LIVE separation, persistent evidence and audit support, a separate position-management loop, counterfactual learning hooks, notification failure isolation, and a shared Telegram/Slack control foundation. These should be retained and extended rather than replaced.

## Initial gaps to verify before implementation

1. Whether all strategy decisions pass through one authoritative context-aware router.
2. Whether knowledge concepts, combinations, conflicts, and incremental value are represented as persistent machine-readable evidence.
3. Whether every trade and rejected trade has the complete structured decision record required by the specification.
4. Whether regime vocabulary and top-down alignment are normalized across all analysis paths.
5. Whether small-account economics are enforced consistently from theoretical sizing through broker-normalized volume and margin.
6. Whether champion/challenger/core/quarantined promotion has explicit out-of-sample, forward-DEMO, drawdown, robustness, and rollback criteria.
7. Whether model drift and feature importance are persisted and can reduce authority without silently changing production behavior.
8. Whether individual symbol, strategy, and messaging failures remain isolated from the rest of the system.
9. Whether all research and backtest paths are chronological and free of future-data leakage.
10. Whether the running scheduler actually invokes every newly introduced module rather than leaving dead interfaces.

## Implementation rule

No broad rewrite should occur until the remaining gaps are traced to concrete code paths. Validated safety and broker-integrity components must remain stable under the specification's 95–100% rule.

## Live-path audit observations

The live scheduler imports and invokes `analysis.structure`, supply/demand, scoring, setup validation, strategy selection, opportunity ranking, decision gates, risk, execution, optimizer, research governance, and broker-universe modules. The strategy registry currently contains twelve fixed strategy definitions with regime, timeframe, and required-feature applicability. `strategy.selection.evaluate_strategies` ranks applicable definitions and explicitly describes its scores as descriptive rather than entry authorization.

The final pre-order gate is centralized in `analysis.decision_gates.evaluate_trading_gate`. It separates research decision, trading decision, evidence classification, confidence, strategy status, objective status, hard gates, actual RR, and execution eligibility. The executor remains responsible for final broker checks.

Current contradictions requiring resolution or explicit compatibility treatment include:

- The attached specification states a default hard minimum RR of 1.5 and preferred target RR of 2.0+, while `TradeSettings` currently defaults to `min_rr_ratio=2.0` and `preferred_rr_ratio=3.0`. This must not be changed until the authoritative policy decision is recorded and tests are updated.
- `TradeSettings` still contains multiple legacy daily-risk, trade-count, open-position, and spread fields alongside research-oriented settings. The specification requires these to remain policy variables while preserving technical safeguards; their ownership and optimizer treatment need an explicit audit.
- The current strategy registry is machine-readable at the strategy-definition level but does not yet expose the full specification fields for concepts, combinations, conflicts, feature incremental value, execution requirements, and versioned evidence.
- The current routing is strategy-definition applicability plus scoring; a distinct persistent concept-combination engine and explicit independent-evidence accounting are not yet proven.
- The current objective and account phase systems coexist. The specification requires economic account regimes to remain separate from objective sessions and prohibits artificial phases from overriding market logic; this boundary needs tests.
- The communication architecture is centralized for the new shared command and notification paths, but specialized legacy Telegram commands remain outside the shared command bus and must be migrated or explicitly documented.
- The latest resume fix is validated in the repository but requires VPS deployment and runtime confirmation; it must not be conflated with the intelligence rebuild.

## Audit status

The repository is sufficiently mapped to begin a first implementation slice, but the full specification is substantially broader than a single patch. Work should proceed by adding explicit contracts and tests, then migrating live scheduler calls so no new module remains unused.
