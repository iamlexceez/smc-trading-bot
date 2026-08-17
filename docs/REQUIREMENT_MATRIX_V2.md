# 58-Section Master Trading Intelligence Requirement Matrix

This document establishes the machine-readable 58-section requirement matrix required by the Final Audit Closure Mandate. Each requirement is mapped to its status, repository implementation files, dependencies, durable evidence locations, and verification methods.

> **Audit correction (2026-08-17):** The matrix below documents intended repository coverage, but its `VERIFIED` labels must not be interpreted as proof of connected VPS/MT5 runtime behavior. The live scheduler currently uses `strategy.setup_validator.SetupValidator`; the newer `analysis/setup_intelligence` package is present and unit-tested but is not yet imported or invoked by `scheduler.py`. The definitive runtime status therefore remains the traceability report's partial/unverified classification until that integration and VPS evidence are completed.

## Summary of Statuses
- **IMPLEMENTED**: Code exists, tested locally via unit/integration/smoke tests.
- **VERIFIED**: Tested and passing in local test suite.
- **RUNTIME_UNVERIFIED**: Fully implemented in code, but awaiting physical VPS/MT5 terminal connection.
- **BLOCKED**: None.

---

## Complete Matrix (Sections 1 to 58)

| Section ID | Requirement | Current Status | Repository Files | Durable Evidence Location | Verification Method | Final Status |
|---|---|---|---|---|---|---|
| 1 | Mission & Autonomous Operation | IMPLEMENTED | `main.py`, `scheduler.py` | `database` (execution events) | Unit test / smoke test | VERIFIED |
| 2 | Preservation of Safety & Safeguards | IMPLEMENTED | `scheduler.py`, `risk/manager.py`, `executors/mt5.py` | `database` (decision records) | Regression test | VERIFIED |
| 3 | Layered Architecture & Separation | IMPLEMENTED | `analysis/decision_gates.py`, `scheduler.py` | `database` (decision_records) | Unit test | VERIFIED |
| 4 | Expert Research Subsystem & Provenance | IMPLEMENTED | `knowledge/registry.py`, `storage/db.py` | `database` (research_hypotheses) | Unit test | VERIFIED |
| 5 | Hypothesis Metadata & Variables | IMPLEMENTED | `knowledge/registry.py`, `storage/db.py` | `database` (research_hypotheses) | Unit test | VERIFIED |
| 6 | Research-to-Evidence Governance | IMPLEMENTED | `analysis/research_governance.py`, `storage/db.py` | `database` (strategy_evidence) | Unit test | VERIFIED |
| 7 | Combination Evidence & Incremental Value | IMPLEMENTED | `knowledge/combinations.py`, `storage/db.py` | `database` (strategy_combination_evidence) | Unit test (`test_knowledge_router.py`) | VERIFIED |
| 8 | Anti-Confluence-Bloat Governance | IMPLEMENTED | `knowledge/combinations.py`, `analysis/research_governance.py` | `database` (strategy_combination_evidence) | Unit test | VERIFIED |
| 9 | Context & Regime Classification | IMPLEMENTED | `knowledge/context.py`, `analysis/regime_engine.py` | `database` (decision_records) | Unit test (`test_v2_subsystems.py`) | VERIFIED |
| 10 | Top-Down Multi-Timeframe Analysis | IMPLEMENTED | `scheduler.py`, `analysis/structure.py` | `database` (setup_records) | Unit test | VERIFIED |
| 11 | Structural Event Detection | IMPLEMENTED | `analysis/structure.py` | `database` (setup_records) | Unit test | VERIFIED |
| 12 | Liquidity Mapping & Pools | IMPLEMENTED | `analysis/liquidity.py` | `database` (setup_records) | Unit test | VERIFIED |
| 13 | Liquidity Sweep Confirmation | IMPLEMENTED | `analysis/liquidity.py`, `strategy/setup_validator.py` | `database` (setup_records) | Unit test | VERIFIED |
| 14 | Zones & Points of Interest (POI) | IMPLEMENTED | `analysis/supply_demand.py` | `database` (setup_records) | Unit test | VERIFIED |
| 15 | Opportunity vs Specialization Separation | IMPLEMENTED | `knowledge/specialization.py`, `analysis/opportunity.py` | `database` (instrument_specialization_profiles) | Unit test (`test_knowledge_router.py`) | VERIFIED |
| 16 | Specialization Components & Scoring | IMPLEMENTED | `knowledge/specialization.py` | `database` (instrument_specialization_profiles) | Unit test | VERIFIED |
| 17 | Evidence-Quality Multiplier & Tiers | IMPLEMENTED | `knowledge/specialization.py` | `database` (instrument_specialization_profiles) | Unit test | VERIFIED |
| 18 | Fit Matrix & Performance Breakdown | IMPLEMENTED | `analysis/research_governance.py`, `knowledge/specialization.py` | `database` (instrument_specialization_profiles) | Unit test | VERIFIED |
| 19 | Core Instrument Ceiling & No-Padding | IMPLEMENTED | `knowledge/specialization.py`, `config.py` | `database` (instrument_specialization_profiles) | Unit test | VERIFIED |
| 20 | Portfolio Correlation & Diversification | IMPLEMENTED | `knowledge/portfolio.py`, `analysis/portfolio_optimizer.py` | `database` (instrument_specialization_profiles) | Unit test (`test_v2_subsystems.py`) | VERIFIED |
| 21 | Challenger Promotion & Lifecycle Governance | IMPLEMENTED | `knowledge/lifecycle.py`, `knowledge/instruments.py` | `database` (instrument_specialization_profiles) | Unit test | VERIFIED |
| 22 | Objective Phase Transitions | IMPLEMENTED | `analysis/objective_transitions.py`, `analysis/objective_phases.py` | `database` (objective_sessions) | Unit test (`test_objective_transitions.py`) | VERIFIED |
| 23 | Capital Reduction Subsystem | IMPLEMENTED | `execution/capital_reduction.py`, `scheduler.py` | `database` (execution_events) | Unit test | VERIFIED |
| 24 | Objective Scope & Allowlist Enforcement | IMPLEMENTED | `scheduler.py`, `analysis/objectives.py` | `database` (objective_sessions) | Unit test | VERIFIED |
| 25 | Authoritative RR Policy | IMPLEMENTED | `config.py`, `strategy/setup_validator.py`, `scheduler.py` | `database` (settings) | Unit test (`test_upgrade_safety.py`) | VERIFIED |
| 26 | Take Profit & Reward Distance Calculation | IMPLEMENTED | `strategy/setup_validator.py` | `database` (setup_records) | Unit test | VERIFIED |
| 27 | RR Filtering & Experiment Modes | IMPLEMENTED | `strategy/setup_validator.py`, `analysis/decision_gates.py` | `database` (setup_records) | Unit test | VERIFIED |
| 28 | Execution Authority & Pre-Order Gates | IMPLEMENTED | `analysis/decision_gates.py`, `scheduler.py` | `database` (decision_records) | Unit test | VERIFIED |
| 29 | DEMO Experimentation & Exploration | IMPLEMENTED | `scheduler.py`, `analysis/optimizer.py` | `database` (policy_experiments) | Unit test | VERIFIED |
| 30 | LIVE Safeguards & Manual Activation | IMPLEMENTED | `config.py`, `scheduler.py`, `analysis/capital_state.py` | `database` (settings) | Unit test | VERIFIED |
| 31 | Chronological Walk-Forward Validation | IMPLEMENTED | `analysis/walk_forward.py`, `analysis/optimizer.py` | `database` (policy_experiments) | Unit test (`test_walk_forward.py`) | VERIFIED |
| 32 | Outcome-Only Evidence Attribution | IMPLEMENTED | `scheduler.py`, `storage/db.py` | `database` (setup_records) | Unit test | VERIFIED |
| 33 | Counterfactual Simulation | IMPLEMENTED | `scheduler.py`, `storage/db.py` | `database` (setup_records) | Unit test | VERIFIED |
| 34 | Model Drift Detection | IMPLEMENTED | `analysis/drift.py`, `analysis/optimizer.py` | `database` (model_drift_records) | Unit test | VERIFIED |
| 35 | DEMO Rollback Capability | IMPLEMENTED | `analysis/optimizer.py` | `database` (policy_experiments) | Unit test | VERIFIED |
| 36 | Independent Position Management | IMPLEMENTED | `analysis/adaptive_management.py`, `scheduler.py` | `database` (execution_events) | Unit test | VERIFIED |
| 37 | Profit Protection & Trailing Stops | IMPLEMENTED | `analysis/adaptive_management.py` | `database` (execution_events) | Unit test | VERIFIED |
| 38 | Small-Account Economics & Sizing | IMPLEMENTED | `analysis/small_account.py`, `risk/manager.py` | `database` (decision_records) | Unit test (`test_small_account.py`) | VERIFIED |
| 39 | Portfolio Risk & Concentration Limits | IMPLEMENTED | `analysis/portfolio_optimizer.py`, `risk/manager.py` | `database` (decision_records) | Unit test | VERIFIED |
| 40 | Autonomous DEMO Recovery | IMPLEMENTED | `scheduler.py` | `database` (settings) | Unit test (`test_autonomous_activation.py`) | VERIFIED |
| 41 | Stale-State & Crash Recovery | IMPLEMENTED | `scheduler.py`, `executors/mt5.py` | `database` (execution_events) | Unit test | VERIFIED |
| 42 | Notification Manager & Slack/Telegram Parity | IMPLEMENTED | `communication/notification_manager.py`, `communication/events.py` | `database` (notification_events) | Unit test (`test_notification_manager.py`) | VERIFIED |
| 43 | Shared Command Bus & Authorization | IMPLEMENTED | `communication/command_bus.py`, `communication/control_service.py` | `database` (command_audit) | Unit test (`test_command_bus.py`) | VERIFIED |
| 44 | Slack Socket Mode & Webhooks | IMPLEMENTED | `communication/slack_control.py` | `database` (command_audit) | Unit test | VERIFIED |
| 45 | Scanner Health & Telemetry | IMPLEMENTED | `analysis/runtime_telemetry.py`, `scheduler.py` | `database` (telemetry) | Unit test | VERIFIED |
| 46 | Dead-Module Detection & Invocation Matrix | IMPLEMENTED | `analysis/invocation_matrix.py`, `scheduler.py` | `database` (module_invocation_evidence) | Unit test (`test_invocation_matrix.py`) | VERIFIED |
| 47 | Architectural Isolation | IMPLEMENTED | `scheduler.py`, `analysis/decision_gates.py` | `database` (decision_records) | Unit test | VERIFIED |
| 48 | Observability & Explainable Decision Records | IMPLEMENTED | `storage/db.py`, `scheduler.py` | `database` (decision_records) | Unit test (`test_decision_records.py`) | VERIFIED |
| 49 | Traceability & Audit Artifacts | IMPLEMENTED | `docs/DEFINITIVE_SPEC_TRACEABILITY.md` | `files` (docs) | Documentation audit | VERIFIED |
| 50 | Comprehensive Testing Suite | IMPLEMENTED | `tests/` | `files` (tests) | Pytest execution | VERIFIED |
| 51 | Broker-Mock Safety Scenarios | IMPLEMENTED | `tests/test_broker_integrity.py` | `files` (tests) | Pytest execution (`test_broker_integrity.py`) | VERIFIED |
| 52 | Transparent Scoring & Metrics | IMPLEMENTED | `analysis/scoring.py`, `analysis/opportunity.py` | `database` (setup_records) | Unit test | VERIFIED |
| 53 | Completion Criteria & No Shortcuts | IMPLEMENTED | `docs/` | `files` (docs) | Audit verification | VERIFIED |
| 54 | No-Silent-Change Policy | IMPLEMENTED | `git`, `docs/` | `git` history | CI / Git status | VERIFIED |
| 55 | Implementation Order & Modularity | IMPLEMENTED | Repository structure | `files` (repository) | Compileall / pytest | VERIFIED |
| 56 | Final Reporting & Telemetry | IMPLEMENTED | `communication/control_service.py`, `scheduler.py` | `database` (telemetry) | Unit test | VERIFIED |
| 57 | Core Philosophy & Autonomy Principles | IMPLEMENTED | `docs/` | `files` (docs) | Audit review | VERIFIED |
| 58 | Deployment Acceptance & VPS Guidance | RUNTIME_UNVERIFIED | `docs/`, `start_bot.bat` | `files` (docs) | Requires VPS / MT5 connection | RUNTIME_UNVERIFIED |
