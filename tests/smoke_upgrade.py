"""Deterministic smoke tests for the causal, basket-risk upgrade.

These tests use fixed local fixtures only. They never connect to MT5, Telegram,
or a market-data service, and they cannot place, modify, or close a trade.
"""

from __future__ import annotations

import asyncio
import os
import sys
import tempfile
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pandas as pd

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from config import TradeSettings
from analysis.displacement import detect_displacement
from analysis.indicators import causal_swing_highs
from analysis.structure import analyze_structure
from analysis.supply_demand import detect_sd_zones
from analysis.confirmation import detect_inside_bar_breakout
from analysis.evidence import completed_outcome_statistics
from analysis.expert_knowledge import DEFAULT_EXPERT_HYPOTHESES, evaluate_hypothesis_evidence, evidence_strength
from analysis.liquidity import build_liquidity_pools
from execution.manager import ManagementState, TradeManager
from executors.base import Position
from executors.mt5 import MT5Executor
from risk.manager import RiskManager
from storage import db
from data.universe import DerivMarketUniverse
from data.provider import DataProvider
from analysis.optimizer import SelfOptimizer
from analysis.research_governance import ResearchGovernance
from analysis.adaptive_management import observation_from_broker_trade, observations_from_backtest, summarize_management
from analysis.objectives import ObjectiveInterpreter, ObjectiveValidator, TradingObjective, phase_for_equity, objective_operational_readiness, resolve_requested_symbols
from analysis.objective_phases import plan_objective_phases
from backtest.engine import BacktestEngine, BacktestResult, BacktestTrade
from analysis.policies import ExperimentalPolicy, HypothesisEngine, PolicyEvaluator, PolicyGenerator
from analysis.account_monitor import summarize_history, exposure_summary
from execution.capital_reduction import CapitalReductionEngine
from execution import capital_reduction as capital_reduction_module
from analysis.capital_state import AccountCapitalState, CapitalStateService
from analysis.capital_protection import calculate_capital_protection
from analysis.opportunity import market_context, rank_opportunities
from analysis.decision_gates import classify_confidence, classify_evidence, evaluate_trading_gate
from analysis.runtime_telemetry import RuntimeTelemetry
from strategy.setup_validator import calculate_rr, rr_filter_passes
from strategy.registry import applicable_strategies, definitions
from strategy.selection import evaluate_strategies, evidence_class
import scheduler  # noqa: F401 — validates live-pipeline imports without starting it.
from bot.handlers import BotHandlers, admin_only  # noqa: F401 — validates Telegram control imports.
from bot.capital_views import capital_test_view
from telegram.error import BadRequest


def assert_true(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)


def test_two_gate_decision_architecture() -> None:
    weak = {"sample_size": 0, "evidence_stage": "exploration", "confidence": "UNKNOWN"}
    decision = evaluate_trading_gate(
        setup_valid=True, broker_symbol_valid=True, valid_market_data=True,
        objective_permits_exposure=True, evidence=weak, champion_governed=True,
    )
    assert_true(decision.research_decision == "RESEARCH_ACCEPTED", "research gate did not accept a measurable candidate")
    assert_true(decision.trading_decision == "INSUFFICIENT_EVIDENCE", "under-evidenced candidate was allowed objective trading")
    assert_true(decision.evidence_classification == "INSUFFICIENT" and decision.confidence_classification == "UNVALIDATED", "evidence and confidence were not tracked independently")

    strong = {"sample_size": 80, "evidence_strength": "STRONG_EVIDENCE", "evidence_stage": "forward_demo", "confidence": "VALIDATED"}
    challenger = evaluate_trading_gate(
        setup_valid=True, broker_symbol_valid=True, valid_market_data=True,
        objective_permits_exposure=True, evidence=strong, champion_governed=False,
    )
    assert_true(challenger.trading_decision == "OBJECTIVE_INELIGIBLE", "challenger policy bypassed champion governance")

    score_only = evaluate_trading_gate(
        setup_valid=True, broker_symbol_valid=True, valid_market_data=True,
        objective_permits_exposure=True,
        evidence={"sample_size": 0, "evidence_stage": "exploration", "confidence": "UNKNOWN", "feature_score": 99.9},
        champion_governed=True,
    )
    assert_true(score_only.trading_decision != "TRADE_APPROVED", "feature score alone authorized objective trading")
    assert_true(classify_evidence({"evidence_strength": "PROMISING"}) == "MODERATE", "existing evidence label was not normalized")
    assert_true(classify_confidence({"confidence": "UNKNOWN"}) == "UNVALIDATED", "unknown confidence was fabricated into a validated state")


def test_broker_stop_normalization() -> None:
    buy = MT5Executor._normalise_protective_levels(
        direction="BUY", bid=100.00, ask=100.10, sl=100.08, tp=100.12,
        point=0.01, tick_size=0.01, digits=2, stops_level=10, freeze_level=0,
    )
    assert_true(buy["valid"] and buy["changed"], "BUY stops near ask were not normalized")
    assert_true(buy["sl"] == 100.00 and buy["tp"] == 100.20 and buy["minimum_distance"] == 0.10, "BUY stops were not rounded away from ask by broker stop level")
    sell = MT5Executor._normalise_protective_levels(
        direction="SELL", bid=100.00, ask=100.10, sl=100.03, tp=99.97,
        point=0.01, tick_size=0.01, digits=2, stops_level=5, freeze_level=20,
    )
    assert_true(sell["valid"] and sell["changed"], "SELL stops near bid were not normalized")
    assert_true(sell["sl"] == 100.20 and sell["tp"] == 99.80 and sell["minimum_distance"] == 0.20, "SELL stops did not honor the larger freeze distance and tick rounding")
    invalid = MT5Executor._normalise_protective_levels(
        direction="BUY", bid=100.00, ask=100.10, sl=0.0, tp=100.20,
        point=0.01, tick_size=0.01, digits=2, stops_level=10, freeze_level=0,
    )
    assert_true(not invalid["valid"] and "positive" in invalid["reason"], "missing SL/TP did not fail before order submission")
    assert_true(MT5Executor._order_check_succeeded(SimpleNamespace(retcode=0, comment="Done"), 10009), "MT5 order_check retcode 0/comment Done was not treated as successful preflight")
    assert_true(MT5Executor._order_check_succeeded(SimpleNamespace(retcode=10009, comment=""), 10009), "standard MT5 DONE order-check response was not accepted")
    assert_true(not MT5Executor._order_check_succeeded(SimpleNamespace(retcode=0, comment="Invalid stops"), 10009), "non-success zero order-check response was incorrectly accepted")
    buffered_buy = MT5Executor._expand_protective_levels(direction="BUY", sl=100.00, tp=100.20, tick_size=0.01, digits=2, extra_ticks=4)
    buffered_sell = MT5Executor._expand_protective_levels(direction="SELL", sl=100.20, tp=99.80, tick_size=0.01, digits=2, extra_ticks=4)
    assert_true(buffered_buy[0] <= 99.96 and buffered_buy[1] >= 100.24 and buffered_sell[0] >= 100.24 and buffered_sell[1] <= 99.76, "broker order-check stop buffer did not expand protection away from entry")


def test_opportunity_context_and_ranking() -> None:
    closes = [100.0 + index * 0.35 for index in range(80)]
    frame = pd.DataFrame({
        "open": [value - 0.1 for value in closes], "high": [value + 0.25 for value in closes],
        "low": [value - 0.25 for value in closes], "close": closes,
    })
    context = market_context(frame)
    assert_true(context["regime"] in {"COMPRESSION", "EXPANSION", "TRENDING", "RANGING", "EXHAUSTION", "TRANSITION"}, "closed-candle regime classification returned an invalid label")
    candidates = [SimpleNamespace(symbol="Boom 100 Index", score=80.0), SimpleNamespace(symbol="Boom 500 Index", score=90.0)]
    contexts = {item.symbol: {"regime": "TRENDING", "adx": 30.0, "atr_ratio": 1.0, "momentum": 0.4} for item in candidates}
    profiles = {item.symbol: SimpleNamespace(expectancy_r=0.2) for item in candidates}
    historical = {item.symbol: {"sample_size": 20, "expectancy_r": 0.2} for item in candidates}
    ranked = rank_opportunities(candidates, profiles=profiles, contexts=contexts, historical=historical, open_symbols=["Boom 500 Index"])
    assert_true(ranked[0].symbol == "Boom 100 Index" and ranked[0].classification == "BEST_OPPORTUNITY", "portfolio-aware ranking did not penalize duplicate same-instrument exposure")
    assert_true("existing same-instrument exposure" in ranked[-1].rationale, "portfolio conflict was not retained in the opportunity thesis rationale")
    assert_true("strategy" in ranked[0].details and "confidence" in ranked[0].details and "thesis" in ranked[0].details, "opportunity board did not retain complete strategy thesis details")


def test_capacity_aware_opportunity_selection() -> None:
    def candidate(symbol: str, evidence: dict, policy: dict | None = None) -> SimpleNamespace:
        return SimpleNamespace(
            symbol=symbol, score=85.0, selected_strategy="continuation", strategy_score=80.0,
            strategy_evidence=evidence, expected_value_r=0.5, entry_price=100.0,
            stop_loss=99.0, take_profit=102.0, direction="BUY", timeframe="M15",
            htf_bias=["BULLISH"], layering_suitable=False,
            experimental_policy=policy or {"max_positions": 1, "low_capital_entry_model": "high_confidence_only"},
        )

    contexts = {
        "Weak Evidence": {"regime": "TRENDING", "adx": 30.0, "atr_ratio": 1.0, "momentum": 0.4, "return_signature": [0.01, 0.02, 0.01, 0.02]},
        "Strong Evidence": {"regime": "TRENDING", "adx": 30.0, "atr_ratio": 1.0, "momentum": 0.4, "return_signature": [0.01, 0.02, 0.01, 0.02]},
    }
    ranked = rank_opportunities(
        [
            candidate("Weak Evidence", {"sample_size": 0}),
            candidate("Strong Evidence", {"sample_size": 10, "expectancy_r": 0.5, "recent_expectancy_r": 0.4, "expectancy_ci95_low_r": 0.1}),
        ],
        profiles={}, contexts=contexts,
        historical={},
        capacity_context={"account_state": "LOW_CAPITAL", "low_capital": True, "new_exposure_allowed": True, "open_position_count": 0, "minimum_evidence_sample": 10},
    )
    by_symbol = {item.symbol: item for item in ranked}
    assert_true(by_symbol["Strong Evidence"].details["capacity_allowed"], "low-capital selection rejected an A+ evidence-supported opportunity")
    assert_true(not by_symbol["Weak Evidence"].details["capacity_allowed"] and "A+ / high-confidence" in " ".join(by_symbol["Weak Evidence"].details["capacity_reasons"]), "low-capital selection admitted an under-evidenced candidate")
    assert_true(by_symbol["Strong Evidence"].details["maximum_peer_correlation"] is not None, "closed-candle peer correlation was not reported")
    blocked = rank_opportunities(
        [candidate("Blocked", {"sample_size": 50, "expectancy_r": 0.5})], profiles={},
        contexts={"Blocked": contexts["Strong Evidence"]}, historical={},
        capacity_context={"account_state": "MARGIN_PRESSURE", "low_capital": True, "new_exposure_allowed": False, "open_position_count": 0, "minimum_evidence_sample": 10},
    )
    assert_true(not blocked[0].details["capacity_allowed"] and "blocks new exposure" in " ".join(blocked[0].details["capacity_reasons"]), "broker account-state capacity block was not enforced in opportunity selection")


def test_strategy_registry_and_selection() -> None:
    assert_true(len(definitions()) >= 10, "strategy registry did not expose the declared strategy families")
    trending = applicable_strategies("TRENDING", "M15", {"structure_event", "displacement", "htf_alignment"})
    trending_ids = {item.identifier for item in trending}
    assert_true("bos_choch_continuation" in trending_ids and "trend_continuation" in trending_ids, "trending structural setup did not map to continuation strategies")
    assessments = evaluate_strategies(
        regime="TRENDING", timeframe="M15", observed_features={"structure_event", "displacement", "htf_alignment"}, setup_quality=80.0,
        evidence_by_strategy={
            "bos_choch_continuation": {"sample_size": 20, "expectancy_r": 0.35},
            "trend_continuation": {"sample_size": 20, "expectancy_r": -0.25},
        },
    )
    assert_true(assessments and assessments[0].identifier == "bos_choch_continuation", "strategy selection did not prefer stronger matching completed evidence")
    assert_true(evidence_class(0, None) == "UNKNOWN" and evidence_class(4, 0.2) == "EARLY", "evidence confidence misclassified small samples")
    assert_true(evidence_class(20, 0.2) == "PROMISING" and evidence_class(60, -0.2) == "VALIDATED", "confidence bands did not use the documented completed-outcome sample sizes")


def test_forward_demo_evaluation_provenance() -> None:
    rows = [
        {"pnl_r": 1.0, "symbol": "Boom 100 Index", "regime": "TRENDING"},
        {"pnl_r": -0.5, "symbol": "Boom 100 Index", "regime": "TRENDING"},
        {"pnl_r": 0.8, "symbol": "XAUUSD", "regime": "RANGING"},
    ]
    evaluation = SelfOptimizer._realized_forward_evaluation(rows)
    assert_true(evaluation["basis"] == "broker_realized_forward_demo_R_outcomes" and evaluation["provenance"] == "FORWARD_DEMO", "forward-DEMO evaluation did not retain realized-data provenance")
    assert_true(set(evaluation["instrument_partitions"]) == {"Boom 100 Index", "XAUUSD"} and set(evaluation["regime_partitions"]) == {"TRENDING", "RANGING"}, "forward-DEMO evaluation did not retain instrument/regime partitions")


def test_expert_hypothesis_evidence_classifier() -> None:
    assert_true(evidence_strength(0) == "UNKNOWN" and evidence_strength(3) == "EARLY" and evidence_strength(50) == "VALIDATED", "expert hypothesis evidence-depth bands are incorrect")
    early = evaluate_hypothesis_evidence(sample_size=5, expectancy_r=0.8, ci_low_r=-0.5, ci_high_r=1.5, historical_sample_size=5, forward_sample_size=5)
    assert_true(early["decision"] == "INCONCLUSIVE" and not early["promotion_eligible"] and not early["live_promotion_allowed"], "small expert hypothesis samples were overstated")
    robust = evaluate_hypothesis_evidence(sample_size=60, expectancy_r=0.4, ci_low_r=0.1, ci_high_r=0.7, historical_sample_size=40, forward_sample_size=55)
    assert_true(robust["decision"] == "SUPPORTED" and robust["promotion_eligible"] and not robust["live_promotion_allowed"], "robust DEMO hypothesis evidence was not classified conservatively for LIVE")


async def test_expert_knowledge_journal_persistence() -> None:
    with tempfile.TemporaryDirectory() as directory:
        path = os.path.join(directory, "expert_knowledge.db")
        await db.init_db(path)
        inserted = await db.ensure_expert_knowledge_seeded("demo", path)
        journal = await db.get_expert_knowledge_journal("demo", db_path=path)
        assert_true(inserted == len(DEFAULT_EXPERT_HYPOTHESES) and len(journal) == len(DEFAULT_EXPERT_HYPOTHESES), "expert methodology claims were not seeded idempotently")
        assert_true(all(row["classification"] in {"HYPOTHESIS", "LEARNABLE_PARAMETER"} and row["decision"] == "INCONCLUSIVE" and not row["live_promotion_allowed"] for row in journal), "expert claims were seeded as permanent or LIVE-ready rules")
        updated = await db.record_expert_hypothesis_test(
            account_mode="demo", hypothesis_key=DEFAULT_EXPERT_HYPOTHESES[0].key,
            data_tested=["historical_replay", "forward_demo"], sample_size=60,
            historical_sample_size=40, forward_sample_size=55, expectancy_r=0.4,
            ci_low_r=0.1, ci_high_r=0.7, result="Positive result in the tested context.", db_path=path,
        )
        assert_true(updated["decision"] == "SUPPORTED" and updated["evidence_strength"] == "VALIDATED" and not updated["live_promotion_allowed"], "expert hypothesis test result was not persisted with provenance and LIVE prohibition")


async def test_strategy_evidence_persistence() -> None:
    with tempfile.TemporaryDirectory() as directory:
        path = os.path.join(directory, "strategy_evidence.db")
        await db.init_db(path)
        setup_id = await db.record_setup(
            account_mode="demo", symbol="Boom 100 Index", timeframe="M15", direction="BUY",
            setup_type="bos_choch_continuation", status="executed", entry_price=100.0,
            stop_loss=99.0, take_profit=103.0, strategy_id="bos_choch_continuation",
            regime="TRENDING", db_path=path,
        )
        trade_id = await db.record_trade(
            "Boom 100 Index", "BUY", 100.0, 99.0, 103.0, 0.1, 10.0, 3.0,
            "mt5", "{}", setup_id=setup_id, account_mode="demo", db_path=path,
        )
        await db.close_trade(trade_id, 12.0, path, pnl_r=1.2, max_favorable_r=1.6, max_adverse_r=-0.3)
        evidence = await db.upsert_strategy_evidence(
            "demo", "Boom 100 Index", "bos_choch_continuation", "TRENDING", "M15", db_path=path,
        )
        assert_true(evidence["sample_size"] == 1 and evidence["wins"] == 1 and evidence["expectancy_r"] == 1.2, "strategy evidence did not aggregate completed outcome correctly")
        repeated = await db.upsert_strategy_evidence(
            "demo", "Boom 100 Index", "bos_choch_continuation", "TRENDING", "M15", db_path=path,
        )
        assert_true(repeated["sample_size"] == 1, "rebuilding strategy evidence duplicated a reconciled trade")
        lookup = await db.get_strategy_evidence("demo", "Boom 100 Index", "bos_choch_continuation", "TRENDING", "M15", path)
        assert_true(lookup["confidence"] == "UNKNOWN" and lookup["average_mae_r"] == -0.3 and lookup["average_mfe_r"] == 1.6, "strategy evidence lost MAE/MFE or documented confidence")


async def test_engine_scanner_gate_rendering() -> None:
    handler = object.__new__(BotHandlers)
    telemetry = RuntimeTelemetry()
    handler.scheduler = SimpleNamespace(
        telemetry=telemetry,
        scheduled_task_status=lambda: [],
        last_scan_gate={
            "state": "BROKER_UNIVERSE_EMPTY", "reason": "No broker-valid symbols.",
            "updated_at": "2026-08-14T00:00:00", "analysis_symbols": 0,
        },
        last_opportunity_ranking=[],
    )
    captured: dict[str, str] = {}
    async def render(_update, text: str):
        captured["text"] = text
    handler._render_plain_menu = render
    update = SimpleNamespace(
        effective_user=SimpleNamespace(id=1),
        message=SimpleNamespace(text="/engine"), callback_query=None,
    )
    with patch("bot.handlers.is_admin", return_value=True):
        await handler.cmd_engine(update, SimpleNamespace())
    assert_true("LATEST SCANNER GATE" in captured.get("text", ""), "engine report omitted the scanner-gate section")
    assert_true("BROKER_UNIVERSE_EMPTY" in captured.get("text", ""), "engine report did not render the latest scanner gate")


def test_pause_resume_command_registration() -> None:
    bot_handler = object.__new__(BotHandlers)
    registered = []
    app = SimpleNamespace(add_handler=lambda handler: registered.append(handler))
    BotHandlers.setup(bot_handler, app)
    command_sets = [set(getattr(handler, "commands", set())) for handler in registered]
    assert_true(any("resume" in commands for commands in command_sets), "implemented /resume handler is absent from Telegram command registration")
    assert_true(any("pause" in commands for commands in command_sets), "implemented /pause handler is absent from Telegram command registration")
    assert_true(any("knowledge" in commands for commands in command_sets), "implemented /knowledge handler is absent from Telegram command registration")


def test_pause_recovery_policy() -> None:
    settings = TradeSettings.defaults()
    settings.auto_trade = True
    settings.is_paused = True
    verified = {"state": AccountCapitalState.ACCOUNT_VERIFIED}
    standalone = {"context": {"operational": {"scope_disabled": True}}}

    settings.automation_pause_reason = "LEGACY_STALE"
    assert_true(scheduler.MarketScheduler._pause_recovery_allowed(settings, verified, standalone), "legacy stale pause was not recoverable in standalone DEMO mode")

    settings.automation_pause_reason = "ACCOUNT_SAFETY"
    assert_true(scheduler.MarketScheduler._pause_recovery_allowed(settings, verified, None), "broker-safety pause was not recoverable after verified account recovery")

    settings.automation_pause_reason = "MANUAL"
    assert_true(not scheduler.MarketScheduler._pause_recovery_allowed(settings, verified, standalone), "manual pause was overridden automatically")

    settings.automation_pause_reason = "EMERGENCY_STOP"
    assert_true(not scheduler.MarketScheduler._pause_recovery_allowed(settings, verified, standalone), "emergency pause was overridden automatically")

    settings.automation_pause_reason = "LEGACY_STALE"
    assert_true(not scheduler.MarketScheduler._pause_recovery_allowed(settings, {"state": AccountCapitalState.ACCOUNT_STATE_UNKNOWN}, standalone), "unknown broker state cleared a pause")

    settings.automation_pause_reason = "OBJECTIVE_AWAITING_START"
    assert_true(not scheduler.MarketScheduler._pause_recovery_allowed(settings, verified, None), "objective awaiting explicit start was bypassed")


async def test_immediate_scan_result_reporting() -> None:
    engine = object.__new__(scheduler.MarketScheduler)
    engine.refresh_market_universe = AsyncMock(return_value=False)
    failed = await engine.activate_and_scan_now()
    assert_true(not failed["ok"] and failed["scan"]["state"] == "BROKER_UNIVERSE_EMPTY", "immediate scan did not report broker-refresh failure")

    engine.refresh_market_universe = AsyncMock(return_value=True)
    engine.scan_and_execute = AsyncMock(return_value={"state": "ANALYZING"})
    engine._last_scan_disposition = {"state": "ANALYZING", "symbols_attempted": 3, "symbols_analyzed": 3}
    completed = await engine.activate_and_scan_now()
    assert_true(completed["ok"] and completed["scan"]["state"] == "ANALYZING", "immediate scan did not return the completed scan state")
    assert_true(completed["disposition"]["symbols_attempted"] == 3, "immediate scan did not return scan accounting")

    engine.scan_and_execute = AsyncMock(return_value={"state": "SKIPPED_OVERLAP"})
    engine._last_scan_disposition = {"state": "PAUSED", "reason": "overlap", "symbols_attempted": 0}
    overlap = await engine.activate_and_scan_now()
    assert_true(overlap["scan"]["state"] == "SKIPPED_OVERLAP", "immediate scan did not report overlap")


def test_scanner_gate_telemetry() -> None:
    probe = object.__new__(scheduler.MarketScheduler)
    probe.last_scan_gate = {}
    scheduler.MarketScheduler._set_scan_gate(
        probe, "BROKER_UNIVERSE_EMPTY", "No broker-valid symbols.", analysis_symbols=0,
    )
    assert_true(probe.last_scan_gate["state"] == "BROKER_UNIVERSE_EMPTY", "scanner gate did not retain the exact read-only gate state")
    assert_true(probe.last_scan_gate["analysis_symbols"] == 0 and probe.last_scan_gate["updated_at"], "scanner gate did not retain diagnostic details and timestamp")


def test_runtime_telemetry() -> None:
    telemetry = RuntimeTelemetry()
    telemetry.component_started("market_scanner")
    telemetry.increment("scan_cycles_started")
    telemetry.increment("symbols_attempted", 3)
    telemetry.increment("candle_requests", 6)
    telemetry.record_timeframe("M15", 3)
    telemetry.record_candle_request(request_id="req-1", symbol="Volatility 75 Index", timeframe="M15", purpose="analysis", scan_cycle_id="cycle-1", outcome="success")
    telemetry.record_management_reason("NO_ACTION_WAITING_FOR_1R")
    telemetry.record_rejection("No directional structure")
    telemetry.component_succeeded("market_scanner")
    first = telemetry.heartbeat_snapshot_and_reset()
    assert_true(first["window"]["counters"]["scan_cycles_started"] == 1, "heartbeat window lost a real scan start")
    assert_true(first["window"]["counters"]["symbols_attempted"] == 3 and first["window"]["timeframes"]["M15"] == 3, "runtime symbol/timeframe evidence is incorrect")
    assert_true(first["window"]["candle_purposes"]["analysis"] == 1 and first["window"]["candle_samples"][0]["scan_cycle_id"] == "cycle-1", "candle provenance was not retained")
    assert_true(first["window"]["management_reasons"]["NO_ACTION_WAITING_FOR_1R"] == 1, "management no-action reason was not retained")
    after = telemetry.snapshot()
    assert_true(after["window"]["counters"]["scan_cycles_started"] == 0, "heartbeat did not reset its activity window")
    assert_true(after["lifetime"]["counters"]["scan_cycles_started"] == 1, "lifetime telemetry was incorrectly reset")
    telemetry.component_started("analysis_engine")
    telemetry.component_failed("analysis_engine", RuntimeError("fixture failure"))
    assert_true(after["components"]["market_scanner"]["last_success"], "component success state was not retained")
    assert_true(telemetry.snapshot()["components"]["analysis_engine"]["state"] == "FAILED", "component failure was not exposed")
    assert_true(telemetry.snapshot()["components"]["analysis_engine"]["reason"].startswith("RuntimeError"), "component failure reason was not exposed")


def test_scan_disposition_truthfulness() -> None:
    engine = object.__new__(scheduler.MarketScheduler)
    engine._active_scan_cycle_id = "cycle-zero"
    engine._set_scan_disposition("ACCOUNT_BLOCKED", "MARGIN_PRESSURE blocks new exposure", symbols_discovered=92, symbols_targeted=0, symbols_attempted=0)
    assert_true(engine._last_scan_disposition["state"] == "ACCOUNT_BLOCKED", "blocked scan state was not persisted")
    assert_true(engine._last_scan_disposition["symbols_attempted"] == 0 and engine._last_scan_disposition["reason"].startswith("MARGIN_PRESSURE"), "zero-work scan was incorrectly represented as a normal scan")


async def test_candle_purpose_separation() -> None:
    engine = object.__new__(scheduler.MarketScheduler)
    engine.telemetry = RuntimeTelemetry()
    engine._active_scan_cycle_id = "cycle-candles"
    frame = pd.DataFrame([{"time": pd.Timestamp("2026-08-15T00:00:00Z"), "open": 1.0, "high": 1.1, "low": 0.9, "close": 1.0, "tick_volume": 1}])
    engine.data_provider = SimpleNamespace(get_candles=AsyncMock(return_value=frame))
    result = await scheduler.MarketScheduler.fetch_candles(engine, "Volatility 75 Index", "M5", 200, purpose="position_management")
    assert_true(not result.empty, "fixture candle request did not return data")
    metrics = engine.telemetry.snapshot()["lifetime"]
    assert_true(metrics["counters"]["position_management_candle_requests"] == 1, "position-management candle request was not separated")
    assert_true(metrics["candle_purposes"]["position_management"] == 1, "position-management candle purpose was not recorded")
    assert_true(metrics["candle_samples"][0]["scan_cycle_id"] == "cycle-candles" and metrics["candle_samples"][0]["symbol"] == "Volatility 75 Index", "candle request metadata was incomplete")


def test_full_precision_rr_validation() -> None:
    risk, reward, rr = calculate_rr("SELL", 4350.274, 4402.92955, 4343.897)
    assert_true(abs(risk - 52.65555) < 1e-6 and abs(reward - 6.377) < 1e-6, "SELL RR distances were calculated incorrectly")
    assert_true(abs(rr - 0.121107841433619) < 1e-9 and rr < 3.0, "exact low-RR SELL fixture was not calculated at full precision")
    assert_true(rr_filter_passes(rr, 3.0), "a historical 3R value was still allowed to reject the exact low-RR SELL fixture")
    assert_true(rr_filter_passes(rr, 0.0) and rr_filter_passes(rr, -1.0), "actual RR observation was incorrectly treated as a rejection filter")
    buy_risk, buy_reward, buy_rr = calculate_rr("BUY", 100.0, 98.0, 106.0)
    assert_true(buy_risk == 2.0 and buy_reward == 6.0 and buy_rr == 3.0, "BUY RR formula is incorrect")
    assert_true(rr_filter_passes(buy_rr, 3.0) and rr_filter_passes(buy_rr, 3.1), "historical configured RR values still imposed an execution filter")


async def test_single_flight_scan_guard() -> None:
    engine = object.__new__(scheduler.MarketScheduler)
    engine._scan_lock = asyncio.Lock()
    engine.telemetry = RuntimeTelemetry()
    entered = asyncio.Event()
    release = asyncio.Event()

    async def slow_scan():
        entered.set()
        await release.wait()
        return {"completed": True}

    engine._scan_and_execute = slow_scan
    first = asyncio.create_task(engine.scan_and_execute())
    await entered.wait()
    second = await engine.scan_and_execute()
    release.set()
    await first
    counters = engine.telemetry.snapshot()["lifetime"]["counters"]
    assert_true(second.get("skipped") == "scan already running", "overlapping scan was not explicitly skipped")
    assert_true(counters["scan_cycles_started"] == 1 and counters["scan_cycles_completed"] == 1, "single-flight guard allowed an extra scan cycle")
    assert_true(counters["scan_cycles_skipped_overlap"] == 1, "overlap skip was not recorded")


def test_scanner_eligibility_handoff() -> None:
    engine = object.__new__(scheduler.MarketScheduler)
    engine._analysis_eligible_symbols = ()
    handoff = engine._set_analysis_eligible_symbols({"usable_symbols": ["Volatility 75 Index", "XAUUSDmicro"]})
    reloaded = TradeSettings.from_dict({"enabled_symbols": "EURUSD"})
    assert_true(not reloaded.enabled_symbols, "fixture did not reproduce intentional empty persisted-symbol migration")
    assert_true(handoff == ("Volatility 75 Index", "XAUUSDmicro"), "broker-usable scanner handoff lost the returned identifiers")
    assert_true(engine._analysis_symbol_is_eligible("Volatility 75 Index"), "broker-validated scanner symbol was rejected after settings reload")
    assert_true(not engine._analysis_symbol_is_eligible("EURUSD"), "unverified legacy symbol entered scanner eligibility")
    engine._set_analysis_eligible_symbols({"usable_symbols": ["Volatility 15 (1s) Index"]})
    assert_true(engine._analysis_symbol_is_eligible("Volatility 15 (1s) Index"), "fresh broker-verified Volatility 15 (1s) Index handoff was lost")


def test_config_round_trip() -> None:
    settings = TradeSettings.defaults()
    encoded = settings.to_dict()
    restored = TradeSettings.from_dict(encoded)
    assert_true(restored.entry_mode == settings.entry_mode, "entry mode did not round-trip")
    assert_true(restored.layer_allocation == settings.layer_allocation, "layer allocation did not round-trip")
    assert_true(restored.chart_activity_level == "detailed", "detailed chart activity must be the default")

    legacy = TradeSettings.from_dict({"risk_per_trade": 10.0, "max_daily_loss_pct": 20.0, "max_open_positions": 5, "score_threshold": 60.0})
    assert_true(legacy.risk_per_trade == 10.0, "experimental risk per trade should be preserved without hard-coded capping")
    assert_true(legacy.max_daily_loss_pct == 20.0, "experimental daily loss should be preserved")
    assert_true(legacy.max_open_positions == 5, "experimental position cap should be preserved")
    assert_true(legacy.min_setup_score == 0.0, "learning baseline did not remove the quality-score entry gate")

    migrated_markets = TradeSettings.from_dict({"symbols": "EURUSD,XAUUSDmicro", "enabled_symbols": "EURUSD", "available_symbols": "EURUSD"})
    assert_true(not migrated_markets.symbols and not migrated_markets.enabled_symbols, "persisted legacy symbols survived restart migration")


def test_account_monitor_aggregates() -> None:
    history = summarize_history([{"net_profit": 12.0}, {"net_profit": -5.0}, {"net_profit": 0.0}])
    assert_true(history["trades"] == 3 and history["wins"] == 1 and history["losses"] == 1, "broker history statistics are incorrect")
    assert_true(abs(history["net_profit"] - 7.0) < 1e-9, "broker net P/L aggregation is incorrect")
    exposure = exposure_summary({
        "account": {"equity": 1_000.0, "margin": 100.0, "free_margin": 900.0, "margin_level": 1_000.0},
        "positions": [
            {"symbol": "Boom 100 Index", "direction": "BUY", "volume": 0.2, "potential_sl": -10.0, "potential_tp": 20.0},
            {"symbol": "XAUUSD", "direction": "SELL", "volume": 0.1, "potential_sl": -5.0, "potential_tp": 10.0},
        ],
    })
    assert_true(exposure["open_positions"] == 2 and exposure["margin_exposure_pct"] == 10.0, "live exposure aggregation is incorrect")
    assert_true(exposure["potential_sl"] == -15.0 and exposure["potential_tp"] == 30.0, "SL/TP exposure aggregation is incorrect")


def test_risk_sizing_and_layers() -> None:
    settings = TradeSettings.defaults()
    settings.risk_per_trade = 0.75
    settings.max_setup_risk_pct = 1.0
    settings.auto_trade = True
    settings.enabled_symbols = ["TEST"]
    manager = RiskManager(settings)
    symbol_info = {
        "trade_tick_size": 0.01,
        "trade_tick_value": 1.0,
        "trade_contract_size": 1.0,
        "volume_min": 0.01,
        "volume_max": 100.0,
        "volume_step": 0.01,
    }
    sizing = manager.calculate_position_sizing(
        account_equity=10_000,
        free_margin=8_000,
        entry_price=100.0,
        stop_loss=98.0,
        symbol_info=symbol_info,
        leverage=100,
    )
    assert_true(sizing.valid, f"sizing failed: {sizing.reason}")
    assert_true(sizing.required_lot == sizing.final_volume and sizing.required_lot > 0, "required lot was not broker-normalized for execution")
    assert_true(sizing.expected_loss <= sizing.risk_amount + 1e-6, "sizing exceeded budget")
    fixed_volume = manager.calculate_position_sizing(
        account_equity=10_000, free_margin=8_000, entry_price=100.0, stop_loss=98.0,
        symbol_info=symbol_info, leverage=100, risk_model="fixed_volume", fixed_volume=2.5,
    )
    assert_true(fixed_volume.valid and fixed_volume.final_volume == 2.5 and fixed_volume.required_lot == 2.5, "fixed-volume policy was not broker-normalized correctly")
    broker_minimum = manager.calculate_position_sizing(
        account_equity=152.60, free_margin=152.60, entry_price=100.0, stop_loss=89.15,
        symbol_info={
            "trade_tick_size": 0.01, "trade_tick_value": 0.01,
            "volume_min": 0.2, "volume_max": 100.0, "volume_step": 0.01,
            "margin_per_lot": 11.2,
        },
        leverage=100, risk_pct=0.75,
    )
    assert_true(broker_minimum.valid and broker_minimum.final_volume == 0.2, "broker-minimum adaptive sizing did not select the executable lot")
    assert_true(broker_minimum.risk_adapted_to_broker_minimum and broker_minimum.policy_required_lot < broker_minimum.broker_min_lot, "adaptive broker-minimum evidence is missing")
    assert_true(abs(broker_minimum.minimum_required_risk_pct - (broker_minimum.minimum_lot_loss / 152.60 * 100)) < 1e-9, "minimum required risk was not derived from current effective capital")
    margin_block = manager.calculate_position_sizing(
        account_equity=152.60, free_margin=1.0, entry_price=100.0, stop_loss=89.15,
        symbol_info={
            "trade_tick_size": 0.01, "trade_tick_value": 0.01,
            "volume_min": 0.2, "volume_max": 100.0, "volume_step": 0.01,
            "margin_per_lot": 11.2,
        },
        leverage=100, risk_pct=0.75,
    )
    assert_true(not margin_block.valid and margin_block.sizing_code == "MINIMUM_LOT_MARGIN_UNAFFORDABLE", "genuine minimum-lot margin failure was not retained")
    adaptive_check = asyncio.run(manager.check_all(
        symbol="TEST", direction="BUY", score=0.0, rr_ratio=3.0, spread_pips=0.0,
        account_equity=152.60, free_margin=152.60, required_margin=broker_minimum.required_margin,
        today_pnl=0.0, today_trade_count=0, open_position_count=0,
        proposed_setup_risk=broker_minimum.expected_loss, policy={"risk_pct": 0.75, "risk_model": "fixed_pct"},
        adaptive_minimum_risk=True,
    ))
    assert_true(adaptive_check.passed, "adaptive minimum-risk sizing did not preserve broker and margin validation")
    settings.enabled_symbols = []  # Reproduce intentional persisted-list clearing after a settings reload.
    broker_handoff_check = asyncio.run(manager.check_all(
        symbol="Volatility 15 (1s) Index", direction="BUY", score=0.0, rr_ratio=3.0, spread_pips=0.0,
        account_equity=152.60, free_margin=152.60, required_margin=0.0,
        today_pnl=0.0, today_trade_count=0, open_position_count=0,
        broker_verified_symbol=True, broker_eligibility_detail="fresh usable-symbol handoff; symbol=present",
    ))
    assert_true(broker_handoff_check.passed, "fresh broker handoff did not override stale cleared settings symbols")
    broker_absent_check = asyncio.run(manager.check_all(
        symbol="Volatility 15 (1s) Index", direction="BUY", score=0.0, rr_ratio=3.0, spread_pips=0.0,
        account_equity=152.60, free_margin=152.60, required_margin=0.0,
        today_pnl=0.0, today_trade_count=0, open_position_count=0,
        broker_verified_symbol=False, broker_eligibility_detail="fresh usable-symbol handoff; symbol=absent",
    ))
    assert_true(not broker_absent_check.passed and "Broker-verified enabled symbol" in broker_absent_check.reason, "unverified broker handoff was not fail-closed")
    layers = manager.get_layering_plan(sizing.final_volume, 100.0, 98.0, symbol_info)
    assert_true(bool(layers), "layer plan is empty")
    assert_true(sum(layer["lot"] for layer in layers) <= sizing.final_volume + 1e-6, "layers exceed total volume")

    protected = Position(ticket=1, symbol="TEST", direction="BUY", volume=1.0, entry_price=100.0, sl=101.0, tp=105.0)
    assert_true(manager.calculate_position_risk(protected, symbol_info) == 0.0, "profit-protected stop must consume no loss budget")


def test_no_widening_management() -> None:
    manager = TradeManager()
    assert_true(not manager._is_improvement("BUY", 101.0, 100.5, 0.0), "BUY stop widening was accepted")
    assert_true(not manager._is_improvement("SELL", 99.0, 99.5, 0.0), "SELL stop widening was accepted")


def test_adaptive_capital_protection() -> None:
    low_pressure = calculate_capital_protection(
        account={"balance": 100.0, "equity": 105.0, "free_margin": 90.0, "margin": 10.0},
        positions=[{"volume": 0.1, "profit": 5.0}],
        phase={"starting_equity": 100.0, "target_equity": 200.0},
        management_evidence={},
    )
    high_pressure = calculate_capital_protection(
        account={"balance": 100.0, "equity": 45.0, "free_margin": -1.0, "margin": 46.0},
        positions=[{"volume": 0.9, "profit": -12.0}, {"volume": 0.1, "profit": -2.0}],
        phase={"starting_equity": 100.0, "target_equity": 200.0},
        management_evidence={"average_mae_r": 2.0},
    )
    assert_true(high_pressure.score > low_pressure.score and high_pressure.level in {"ELEVATED", "HIGH"}, "protection score did not increase under falling equity and margin pressure")


def test_causal_confirmation_invariants() -> None:
    # The potential high at index 3 cannot be confirmed until three following
    # candles exist; a slice ending earlier must not report it as a swing.
    highs = [1.0, 2.0, 3.0, 10.0, 4.0, 3.0, 2.0]
    lows = [0.0] * len(highs)
    closes = [1.0, 2.0, 3.0, 9.0, 4.0, 3.0, 2.0]
    frame = pd.DataFrame({"open": closes, "high": highs, "low": lows, "close": closes})
    early_mask = causal_swing_highs(frame.iloc[:6], lookback=3)
    complete_mask = causal_swing_highs(frame, lookback=3)
    assert_true(not early_mask.iloc[3], "swing was visible before its confirmation bars closed")
    assert_true(bool(complete_mask.iloc[3]), "confirmed swing was not detected after confirmation bars closed")

    # The last fixed candle is a large bullish closed-candle displacement.
    displacement_frame = pd.DataFrame({
        "open": [100.0] * 20,
        "high": [100.5] * 19 + [104.0],
        "low": [99.5] * 19 + [99.8],
        "close": [100.1] * 19 + [103.8],
    })
    displacement = detect_displacement(displacement_frame, "BUY", body_ratio_min=0.60, range_ratio_min=1.20)
    assert_true(displacement.confirmed, "closed-candle displacement was not detected")


async def test_broker_only_data_provider() -> None:
    provider = DataProvider()
    assert_true(not await provider.init(), "provider without a broker must fail closed")
    assert_true((await provider.get_candles("UnverifiedMarket", "M5", 20)).empty, "provider created fallback candles without a broker")

    class FakeBroker:
        async def is_connected(self) -> bool:
            return True

        async def connect(self) -> bool:
            return True

        async def get_candles(self, symbol: str, timeframe: str, count: int) -> pd.DataFrame:
            return pd.DataFrame({
                "time": pd.date_range("2026-01-01", periods=3, freq="min", tz="UTC"),
                "open": [100.0, 101.0, 102.0],
                "high": [101.0, 102.0, 103.0],
                "low": [99.0, 100.0, 101.0],
                "close": [100.5, 101.5, 102.5],
                "tick_volume": [1, 2, 3],
            })

    broker_provider = DataProvider(FakeBroker(), cache_ttl_seconds=0)
    assert_true(await broker_provider.init(), "connected broker was not accepted")
    candles = await broker_provider.get_candles("Volatility 75 Index", "M1", 3)
    assert_true(len(candles) == 3 and candles.iloc[-1]["close"] == 102.5, "broker candles were not preserved")


async def test_deriv_market_universe() -> None:
    class FakeBroker:
        async def list_symbols(self) -> list[dict]:
            return [
                {"name": "Volatility 75 Index", "description": "Volatility 75 Index", "path": "Synthetic Indices\\Volatility", "trade_mode": 1, "available": True},
                {"name": "DEX 600 UP Index", "description": "DEX 600 UP Index", "path": "Synthetic Indices\\DEX", "trade_mode": 1, "available": True},
                {"name": "Jump 10 Index", "description": "Jump 10 Index", "path": "Synthetic Indices\\Jump", "trade_mode": 1, "available": True},
                {"name": "XAUUSD", "description": "Gold vs US Dollar", "path": "Metals\\Gold", "trade_mode": 1, "available": True},
                {"name": "XAUUSDmicro", "description": "Gold micro", "path": "Metals\\Gold", "trade_mode": 1, "available": True},
                {"name": "XAUEUR", "description": "Gold vs Euro", "path": "Metals\\Gold", "trade_mode": 1, "available": True},
                {"name": "UnsupportedMarket", "description": "Unsupported broker market", "path": "Other\\Market", "trade_mode": 1, "available": True},
                {"name": "EURUSD", "description": "Euro vs US Dollar", "path": "Forex\\Majors", "trade_mode": 1, "available": True},
                {"name": "BTCETH Arbitrage Index", "description": "BTCETH Arbitrage Index", "path": "Synthetic Indices\\Specialty", "trade_mode": 1, "available": True},
                {"name": "Crash 500 Index", "description": "Crash 500 Index", "path": "Synthetic Indices\\Crash", "trade_mode": 0, "available": False},
            ]

    universe = DerivMarketUniverse()
    await universe.refresh(FakeBroker())
    assert_true(universe.available_symbols == ["DEX 600 UP Index", "Jump 10 Index", "Volatility 75 Index", "XAUUSD", "XAUUSDmicro"], "eligible Deriv markets were not classified correctly")
    assert_true(universe.status_for("Crash 500 Index") == "unavailable", "unavailable broker symbol became active")
    assert_true("UnsupportedMarket" in [r.symbol for r in universe.rejected_records], "unsupported broker symbol was not retained for audit")
    assert_true("EURUSD" in [r.symbol for r in universe.rejected_records], "forex rejection was not retained for audit")
    arbitrage = next(record for record in universe.rejected_records if record.symbol == "BTCETH Arbitrage Index")
    assert_true("excluded non-target" in arbitrage.decision_reason, "non-approved synthetic specialty rejection lacked evidence")
    assert_true("XAUUSDmicro" in universe.available_symbols, "Gold micro variant was incorrectly excluded")
    xau_eur = next(record for record in universe.rejected_records if record.symbol == "XAUEUR")
    assert_true("only XAUUSD and XAUUSDmicro" in xau_eur.decision_reason, "non-USD Gold cross was incorrectly accepted")
    with tempfile.TemporaryDirectory() as directory:
        json_path, markdown_path = universe.write_audit_report(directory)
        assert_true(json_path.exists() and markdown_path.exists(), "complete MT5 symbol audit files were not written")


async def test_basket_persistence() -> None:
    with tempfile.TemporaryDirectory() as directory:
        path = os.path.join(directory, "smoke.db")
        await db.init_db(path)
        basket_id = await db.create_trade_basket(
            symbol="TEST",
            direction="BUY",
            entry_price=100.0,
            initial_stop=98.0,
            initial_target=106.0,
            max_risk=75.0,
            reserved_risk=75.0,
            planned_layers=[{"number": 1, "lot": 0.30}, {"number": 2, "lot": 0.20}],
            db_path=path,
        )
        await db.record_trade_layer(
            basket_id=basket_id,
            ticket=12345,
            layer_number=1,
            planned_volume=0.30,
            executed_volume=0.30,
            entry_price=100.0,
            stop_loss=98.0,
            take_profit=106.0,
            status="open",
            db_path=path,
        )
        basket = await db.get_basket_for_ticket(12345, db_path=path)
        assert_true(basket is not None and basket["id"] == basket_id, "ticket was not mapped to its basket")
        await db.log_basket_action(
            basket_id=basket_id,
            ticket=12345,
            action="Partial Take Profit",
            details={"volume": 0.06},
            db_path=path,
        )
        assert_true(await db.basket_has_action(basket_id, "Partial Take Profit", path), "basket action was not persisted")


async def test_learning_telemetry_persistence() -> None:
    with tempfile.TemporaryDirectory() as directory:
        path = os.path.join(directory, "learning.db")
        await db.init_db(path)
        setup_id = await db.record_setup(
            account_mode="demo",
            symbol="Volatility 75 Index",
            timeframe="M5",
            direction="BUY",
            setup_type="Liquidity Sweep Reversal",
            status="candidate",
            entry_price=100.0,
            stop_loss=98.0,
            take_profit=106.0,
            rr_ratio=3.0,
            quality_score=82.0,
            validation={"zone": {"source": "order_block"}},
            features={"bar_time": "2026-01-01T00:00:00+00:00"},
            db_path=path,
        )
        trade_id = await db.record_trade(
            "Volatility 75 Index", "BUY", 100.0, 98.0, 106.0, 0.1, 82.0, 3.0, "mt5", "{}",
            account_mode="demo", ticket=777, setup_id=setup_id, initial_risk=10.0, db_path=path,
        )
        await db.update_setup_record(setup_id, status="executed", trade_id=trade_id, db_path=path)
        await db.update_trade_excursions(trade_id, current_r=1.5, db_path=path)
        await db.update_trade_excursions(trade_id, current_r=-0.4, db_path=path)
        await db.close_trade(trade_id, 15.0, path, exit_price=103.0, pnl_r=1.5, max_favorable_r=1.5, max_adverse_r=-0.4)
        await db.update_setup_record(setup_id, status="closed", outcome={"pnl_r": 1.5}, db_path=path)
        await db.record_execution_event(
            account_mode="demo", symbol="Volatility 75 Index", setup_id=setup_id, trade_id=trade_id,
            ticket=777, requested_price=100.0, executed_price=100.1, status="filled", db_path=path,
        )
        metrics = await db.get_symbol_setup_metrics("Volatility 75 Index", "M5", "demo", path)
        assert_true(metrics["all"]["sample_size"] == 1 and metrics["all"]["expectancy_r"] == 1.5, "closed setup outcome was not available to learning")
        await db.upsert_symbol_profile(account_mode="demo", symbol="Volatility 75 Index", timeframe="M5", metrics={"sample_size": 1}, db_path=path)
        profile = await db.get_symbol_profile("Volatility 75 Index", "M5", "demo", path)
        assert_true(profile and profile["sample_size"] == 1, "symbol profile was not persisted")


async def test_model_governance_persistence() -> None:
    with tempfile.TemporaryDirectory() as directory:
        path = os.path.join(directory, "models.db")
        await db.init_db(path)
        await db.create_model_version(
            account_mode="demo", version="model_v001", role="champion", status="active",
            parameters={"min_setup_score": 0.0, "preferred_risk_pct": 0.75},
            performance={"out_of_sample": {"expectancy_r": 0.1}}, reason="Baseline", promoted=True, db_path=path,
        )
        await db.create_model_version(
            account_mode="demo", version="model_v002", role="challenger", status="evaluated", previous_version="model_v001",
            parameters={"min_setup_score": 80.0, "preferred_risk_pct": 2.50},
            performance={"out_of_sample": {"expectancy_r": 0.3}}, reason="Walk-forward improvement", db_path=path,
        )
        await db.activate_model_version("model_v002", account_mode="demo", previous_version="model_v001", db_path=path)
        champion = await db.get_active_model("demo", path)
        assert_true(champion and champion["version"] == "model_v002", "challenger promotion did not create the active champion")
        await db.log_optimization_run(
            account_mode="demo", champion_version="model_v002", challenger_version="model_v001",
            decision="rolled_back", details={"reason": "test"}, db_path=path,
        )
        runs = await db.get_recent_optimization_runs("demo", db_path=path)
        assert_true(runs and runs[0]["decision"] == "rolled_back", "optimization decision was not persisted")

    metric = SelfOptimizer(TradeSettings.defaults())._metric([{"pnl_r": 1.0}, {"pnl_r": -0.5}])
    assert_true(metric["expectancy_r"] == 0.25 and metric["max_drawdown_r"] == 0.5, "risk-adjusted objective metrics are incorrect")


async def test_experiment_engine_persistence() -> None:
    policy = ExperimentalPolicy(
        entry_model="hybrid", required_features=("liquidity_sweep",),
        risk_pct=7.5, rr_target=1.25, max_layers=2,
        layer_style="retracement", max_trades_per_day=None,
        daily_stop_model="none", daily_target_model="none",
    )
    accepted, _ = policy.accepts(score=0.0, rr_ratio=1.5, features={"liquidity_sweep": True})
    rejected, _ = policy.accepts(score=0.0, rr_ratio=1.5, features={"liquidity_sweep": False})
    assert_true(accepted and not rejected, "policy feature hypotheses were not applied explicitly")
    policies = PolicyGenerator().generate(HypothesisEngine().generate([]), limit=100)
    assert_true(any(item.risk_pct == 7.5 for item in policies), "risk search space omitted aggressive experimental values")
    assert_true({1, 2, 3}.issubset({item.max_positions for item in policies}), "concentration challenger search space did not include one-, two-, and three-position alternatives")
    evaluation = PolicyEvaluator.evaluate([{"pnl_r": 2.0}, {"pnl_r": -1.0}, {"pnl_r": 1.0}])
    assert_true(evaluation.sample_size == 3 and evaluation.expectancy_r > 0, "policy evaluation did not use actual R outcomes")

    with tempfile.TemporaryDirectory() as directory:
        path = os.path.join(directory, "experiments.db")
        await db.init_db(path)
        hypothesis_id = await db.upsert_research_hypothesis(
            account_mode="demo", hypothesis_key="rr_policy", statement="Test RR values",
            source="smoke", feature_name=None, candidate_values=[1.0, 2.0], db_path=path,
        )
        experiment_id = await db.create_policy_experiment(
            account_mode="demo", policy_fingerprint=policy.fingerprint, policy=policy.to_dict(),
            hypothesis_id=hypothesis_id, status="forward_demo", model_version="model_v002",
            reason="Smoke forward DEMO", db_path=path,
        )
        await db.update_policy_experiment(
            experiment_id, status="forward_demo", evaluation={"out_of_sample": evaluation.to_dict()},
            reason="Collecting actual DEMO outcomes", model_version="model_v002", db_path=path,
        )
        active = await db.get_active_forward_experiment("demo", path)
        assert_true(active and active["id"] == experiment_id and active["policy"]["risk_pct"] == 7.5, "forward DEMO policy assignment was not persisted")


async def test_sequential_capital_reduction_planning() -> None:
    class BrokerFixture:
        def __init__(self, *, free_margin: float = 1_000.0, valid: bool = True) -> None:
            self.free_margin = free_margin
            self.valid = valid

        async def get_symbol_info(self, symbol):
            if not self.valid:
                return {"last_tick_time": time.time()}
            return {
                "last_tick_time": time.time(), "tick_size": 1.0, "tick_value": 1.0,
                "contract_size": 1.0, "min_lot": 1.0, "max_lot": 10.0, "step_lot": 0.5,
            }

        async def get_symbol_price(self, symbol):
            return 100.0, 101.0

    settings = TradeSettings.defaults()
    settings.enabled_symbols = ["Sequential Index"]
    planner = CapitalReductionEngine(settings, BrokerFixture())
    plan, reason, diagnostic = await planner._plan_round_trip(
        {"free_margin": 1_000.0, "leverage": 10.0}, remaining=25.0, tolerance=0.0,
    )
    assert_true(plan is not None and plan.volume == 10.0 and plan.expected_loss == 10.0, "large reduction was not bounded into a first sequential broker-valid action")
    assert_true(plan.maximum_reduction == 10.0, "maximum broker-valid sequential reduction was not reported")

    small_plan, small_reason, _ = await planner._plan_round_trip(
        {"free_margin": 1_000.0, "leverage": 10.0}, remaining=1.2, tolerance=0.0,
    )
    assert_true(small_plan is not None and small_plan.volume == 1.0 and small_plan.expected_loss == 1.0, "valid minimum-volume final sequential action was incorrectly rejected")

    step_plan, _, _ = await planner._plan_round_trip(
        {"free_margin": 1_000.0, "leverage": 10.0}, remaining=5.4, tolerance=0.0,
    )
    assert_true(step_plan is not None and step_plan.volume == 5.0, "broker volume-step rounding did not floor the sequential action")

    margin_engine = CapitalReductionEngine(settings, BrokerFixture(free_margin=1.0))
    margin_plan, margin_reason, margin_diagnostic = await margin_engine._plan_round_trip(
        {"free_margin": 1.0, "leverage": 10.0}, remaining=5.0, tolerance=0.0,
    )
    assert_true(margin_plan is None and margin_diagnostic.get("best_candidate", {}).get("reason") == "insufficient free margin for broker minimum volume", "insufficient free margin was not diagnosed")

    overshoot_plan, overshoot_reason, overshoot_diagnostic = await planner._plan_round_trip(
        {"free_margin": 1_000.0, "leverage": 10.0}, remaining=0.5, tolerance=0.1,
    )
    assert_true(overshoot_plan is None and ("No executable broker-valid" in overshoot_reason or "closest executable candidate" in overshoot_reason), "minimum-loss action outside a zero overshoot envelope was not blocked")

    settings.enabled_symbols = []
    none_engine = CapitalReductionEngine(settings, BrokerFixture())
    none_plan, none_reason, none_diagnostic = await none_engine._plan_round_trip(
        {"free_margin": 1_000.0, "leverage": 10.0}, remaining=5.0, tolerance=0.0,
    )
    assert_true(none_plan is None and none_diagnostic.get("best_candidate") is None, "empty broker universe did not fail with diagnostics")

    settings.enabled_symbols = ["Invalid Index"]
    invalid_engine = CapitalReductionEngine(settings, BrokerFixture(valid=False))
    invalid_plan, invalid_reason, invalid_diagnostic = await invalid_engine._plan_round_trip(
        {"free_margin": 1_000.0, "leverage": 10.0}, remaining=5.0, tolerance=0.0,
    )
    assert_true(invalid_plan is None and "incomplete broker" in invalid_diagnostic["best_candidate"]["reason"], "broker specification failure was not retained in diagnostics")
    assert_true(CapitalReductionEngine._effective_tolerance(500.0, 10.0, 3.0) == 15.0, "effective tolerance did not use the greater target-relative amount")

    class AggressiveBroker(BrokerFixture):
        async def get_symbol_price(self, symbol):
            return (100.0, 105.0) if symbol == "Fast Index" else (100.0, 101.0)

    # The isolated reduction engine uses the scheduler-provided fresh usable
    # handoff rather than normal scan selection, and chooses the largest valid
    # estimated reduction across that complete broker-valid set.
    settings.enabled_symbols = ["Sequential Index"]
    aggressive = CapitalReductionEngine(settings, AggressiveBroker())
    aggressive.broker_usable_symbols = ("Slow Index", "Fast Index")
    fastest, fastest_reason, fastest_diagnostic = await aggressive._plan_round_trip(
        {"free_margin": 1_000.0, "leverage": 10.0}, remaining=25.0, tolerance=0.0, overshoot_tolerance=0.0,
    )
    assert_true(fastest is not None and fastest.symbol == "Fast Index" and fastest.expected_loss == 25.0, "aggressive planner did not choose the largest valid reduction candidate")
    assert_true(fastest_diagnostic["valid_candidate_count"] == 2 and ("target-proximity taper" in fastest_diagnostic["best_candidate"]["reason"] or "Closest valid reduction candidate" in fastest_diagnostic["best_candidate"]["reason"]), "aggressive diagnostics did not retain candidate ranking evidence")
    overshoot_allowed, _, overshoot_allowed_diagnostic = await aggressive._plan_round_trip(
        {"free_margin": 1_000.0, "leverage": 10.0}, remaining=8.0, tolerance=0.0, overshoot_tolerance=2.0,
    )
    assert_true(overshoot_allowed is not None and overshoot_allowed.symbol == "Fast Index" and overshoot_allowed.expected_loss == 10.0, "bounded overshoot did not permit the strongest valid final reduction")
    far_plan, _, far_diagnostic = await aggressive._plan_round_trip(
        {"free_margin": 1_000.0, "leverage": 10.0}, remaining=100.0, tolerance=0.0,
        overshoot_tolerance=100.0, initial_required_reduction=100.0,
    )
    near_plan, _, near_diagnostic = await aggressive._plan_round_trip(
        {"free_margin": 1_000.0, "leverage": 10.0}, remaining=10.0, tolerance=0.0,
        overshoot_tolerance=100.0, initial_required_reduction=100.0,
    )
    assert_true(far_plan is not None and near_plan is not None and far_plan.expected_loss > near_plan.expected_loss, "target-proximity taper did not reduce the selected action near the target")
    assert_true(far_diagnostic["aggression_factor"] == 1.0 and near_diagnostic["aggression_factor"] < 0.02, "target-proximity aggression factor did not tighten quadratically")
    assert_true(near_diagnostic["tapered_overshoot_tolerance"] < far_diagnostic["tapered_overshoot_tolerance"], "target-proximity taper did not shrink the permitted overshoot")

    class SequentialExecutor(BrokerFixture):
        def __init__(self) -> None:
            super().__init__()
            self.accounts = iter([
                {"broker_account_mode": "demo", "equity": 100.0, "balance": 100.0, "free_margin": 1_000.0, "leverage": 10.0},
                {"broker_account_mode": "demo", "equity": 94.0, "balance": 94.0, "free_margin": 1_000.0, "leverage": 10.0},
                {"broker_account_mode": "demo", "equity": 94.0, "balance": 94.0, "free_margin": 1_000.0, "leverage": 10.0},
                {"broker_account_mode": "demo", "equity": 88.0, "balance": 88.0, "free_margin": 1_000.0, "leverage": 10.0},
            ])
            self.submissions = 0
            self.orders: list[dict] = []

        async def get_account_info(self):
            return next(self.accounts)

        async def execute_trade(self, **kwargs):
            self.submissions += 1
            self.orders.append(dict(kwargs))
            if float(kwargs.get("sl") or 0.0) <= 0 or float(kwargs.get("tp") or 0.0) <= 0:
                return SimpleNamespace(success=False, message="fixture rejected non-positive protection")
            return SimpleNamespace(success=True, ticket=self.submissions, entry_price=101.0, message="")

        async def close_position(self, ticket):
            return True

        async def get_closed_position_outcome(self, ticket):
            return {"pnl": -6.0, "exit_price": 100.0, "closed_deals": [ticket]}

    settings.enabled_symbols = ["Sequential Index"]
    executor = SequentialExecutor()
    sequential = CapitalReductionEngine(settings, executor)
    session = {"id": 1, "status": "active", "target_equity": 80.0, "tolerance": 0.0, "metadata": {"tolerance_percent": 0.0}}
    actions: list[dict] = []

    async def record_action(**kwargs):
        actions.append(kwargs)
        return len(actions)

    with patch.object(capital_reduction_module.db, "get_active_capital_reduction_session", new=AsyncMock(return_value=session)), \
         patch.object(capital_reduction_module.db, "update_capital_reduction_session", new=AsyncMock()), \
         patch.object(capital_reduction_module.db, "record_capital_reduction_action", new=record_action):
        first = await sequential.run_once()
        second = await sequential.run_once()
    assert_true(first["state"] == "waiting" and second["state"] == "waiting" and executor.submissions == 2, "large reduction did not progress through sequential broker-valid rounds")
    closed = [row for row in actions if row.get("action") == "round_trip_closed"]
    assert_true([row.get("equity_after") for row in closed] == [94.0, 88.0], "sequential reduction did not record actual broker equity after each close")
    assert_true(first["expected_loss"] != first["equity_before"] - first["equity_after"], "test fixture did not prove realized account movement is distinct from the estimate")
    assert_true(all(float(order["sl"]) > 0 and float(order["tp"]) > 0 for order in executor.orders), "capital reduction submitted a non-positive SL/TP instead of broker-valid emergency protection")

    class ImmediateCloseExecutor(SequentialExecutor):
        def __init__(self) -> None:
            super().__init__()
            self.immediate_submissions = 0
            self.protected_submissions = 0

        async def execute_immediate_close_order(self, **kwargs):
            self.immediate_submissions += 1
            return SimpleNamespace(success=True, ticket=700 + self.immediate_submissions, entry_price=101.0, message="")

        async def execute_trade(self, **kwargs):
            self.protected_submissions += 1
            return SimpleNamespace(success=False, message="protected fallback should not be used")

    immediate_executor = ImmediateCloseExecutor()
    immediate_session = {"id": 77, "status": "active", "target_equity": 80.0, "tolerance": 0.0, "metadata": {"tolerance_percent": 0.0}}
    immediate_actions: list[dict] = []
    async def record_immediate_action(**kwargs):
        immediate_actions.append(kwargs)
        return len(immediate_actions)
    with patch.object(capital_reduction_module.db, "get_active_capital_reduction_session", new=AsyncMock(return_value=immediate_session)), \
         patch.object(capital_reduction_module.db, "update_capital_reduction_session", new=AsyncMock()), \
         patch.object(capital_reduction_module.db, "record_capital_reduction_action", new=record_immediate_action):
        immediate_result = await CapitalReductionEngine(settings, immediate_executor).run_once()
    filled = next(row for row in immediate_actions if row.get("action") == "order_filled")
    assert_true(immediate_result["state"] == "waiting" and immediate_executor.immediate_submissions == 1 and immediate_executor.protected_submissions == 0, "capital reduction did not use the dedicated immediate-close route")
    assert_true(filled["details"]["submission"]["route"] == "broker_preflighted_immediate_close", "capital reduction did not retain immediate-close submission evidence")

    invalid_target_session = {"id": 99, "status": "active", "target_equity": 0.0, "tolerance": 0.0, "metadata": {}}
    invalid_executor = SequentialExecutor()
    with patch.object(capital_reduction_module.db, "get_active_capital_reduction_session", new=AsyncMock(return_value=invalid_target_session)), \
         patch.object(capital_reduction_module.db, "update_capital_reduction_session", new=AsyncMock()), \
         patch.object(capital_reduction_module.db, "record_capital_reduction_action", new=AsyncMock()):
        invalid_target = await CapitalReductionEngine(settings, invalid_executor).run_once()
    assert_true(invalid_target["state"] == "failed" and invalid_executor.submissions == 0 and "non-positive" in invalid_target["reason"], "zero persisted target reached order planning or broker submission")

    tolerance_session = {"id": 2, "status": "active", "target_equity": 80.0, "tolerance": 1.0, "metadata": {"tolerance_percent": 0.0}}
    class ToleranceExecutor(SequentialExecutor):
        async def get_account_info(self):
            return {"broker_account_mode": "demo", "equity": 81.0, "balance": 81.0, "free_margin": 1_000.0, "leverage": 10.0}
    with patch.object(capital_reduction_module.db, "get_active_capital_reduction_session", new=AsyncMock(return_value=tolerance_session)), \
         patch.object(capital_reduction_module.db, "update_capital_reduction_session", new=AsyncMock()), \
         patch.object(capital_reduction_module.db, "record_capital_reduction_action", new=AsyncMock()):
        completed = await CapitalReductionEngine(settings, ToleranceExecutor()).run_once()
    assert_true(completed["state"] == "completed", "configured tolerance did not complete an already-close session")


async def test_chart_activity_notifications() -> None:
    class FakeBot:
        def __init__(self) -> None:
            self.messages: list[str] = []

        async def send_message(self, chat_id: int, message: str) -> None:
            self.messages.append(message)

    class FakeApp:
        def __init__(self) -> None:
            self.bot = FakeBot()

    settings = TradeSettings.defaults()
    settings.chart_activity_notifications = True
    settings.chart_activity_level = "detailed"
    settings.chart_activity_cooldown_seconds = 300
    app = FakeApp()
    live = scheduler.MarketScheduler(settings, object(), object(), bot_app=app, admin_chat_id=1)

    sent = await live._chart_activity("study_started", "TEST", "first", fingerprint="bar-1")
    duplicate = await live._chart_activity("study_started", "TEST", "duplicate", fingerprint="bar-1")
    throttled = await live._chart_activity("study_started", "TEST", "new but throttled", fingerprint="bar-2")
    assert_true(sent and not duplicate and not throttled, "chart-study duplicate suppression failed")
    assert_true(app.bot.messages == ["first"], "suppressed chart alerts reached Telegram")

    settings.chart_activity_level = "essential"
    suppressed = await live._chart_activity("structure_mapped", "TEST", "detail", fingerprint="structure-1")
    essential = await live._chart_activity("broker_submission", "TEST", "essential", fingerprint="submit-1", essential=True)
    assert_true(not suppressed and essential, "essential chart-activity mode did not filter detailed events")
    assert_true(app.bot.messages[-1] == "essential", "essential event was not delivered")


async def test_capital_reduction_isolation() -> None:
    class LiveModeExecutor:
        async def get_account_info(self):
            return {"broker_account_mode": "live", "equity": 10_000.0, "balance": 10_000.0}

    blocked_engine = CapitalReductionEngine(TradeSettings.defaults(), LiveModeExecutor())
    account, reason = await blocked_engine._live_account()
    assert_true(account is None and "not DEMO" in reason, "capital reduction did not hard-block a broker-reported LIVE account")

    with tempfile.TemporaryDirectory() as directory:
        path = os.path.join(directory, "capital_reduction.db")
        await db.init_db(path)
        session_id = await db.create_capital_reduction_session(
            broker_login="123456", target_equity=500.0, tolerance=10.0,
            initial_equity=10_000.0, initial_balance=10_000.0, db_path=path,
        )
        active = await db.get_active_capital_reduction_session("demo", path)
        assert_true(active and active["id"] == session_id and active["status"] == "active", "capital reduction session was not isolated and persisted")
        assert_true(not any("phase" in key.lower() for key in active.keys()), "capital reduction session incorrectly persisted a phase field")
        assert_true("objective_id" not in active, "capital reduction session incorrectly linked to a growth objective")
        strategy_trade = await db.record_trade("Volatility 75 Index", "BUY", 100.0, 99.0, 102.0, 0.1, 0.0, 2.0, "mt5", "{}", ticket=777, db_path=path)
        await db.close_trade(strategy_trade, -3.0, path)
        await db.record_capital_reduction_action(session_id=session_id, action="order_filled", status="open", ticket=777, symbol="Volatility 75 Index", direction="BUY", volume=0.1, db_path=path)
        outcomes = await db.get_strategy_trade_outcomes_excluding_capital_reduction(db_path=path)
        assert_true(not outcomes, "capital-reduction ticket contaminated strategy-learning outcomes")
        await db.update_capital_reduction_session(session_id, status="completed", current_equity=505.0, current_balance=505.0, capital_test_active=True, db_path=path)
        completed = await db.get_capital_reduction_session(session_id, path)
        assert_true(completed and completed["capital_test_active"] and completed["status"] == "completed", "capital-test transition was not persisted")

    class NoActionExecutor:
        async def get_account_info(self):
            return {"broker_account_mode": "demo", "equity": 1_000.0, "balance": 1_000.0, "free_margin": 1_000.0, "leverage": 10.0}
        async def get_symbol_info(self, symbol):
            raise RuntimeError("temporary broker symbol metadata unavailable")
        async def get_symbol_price(self, symbol):
            raise RuntimeError("temporary broker price unavailable")

    no_action_session = {"id": 404, "status": "active", "target_equity": 500.0, "tolerance": 0.0, "metadata": {}}
    state_updates: list[dict] = []
    async def capture_update(session_id, **kwargs):
        state_updates.append(kwargs)
    with patch.object(capital_reduction_module.db, "get_active_capital_reduction_session", new=AsyncMock(return_value=no_action_session)), \
         patch.object(capital_reduction_module.db, "update_capital_reduction_session", new=capture_update), \
         patch.object(capital_reduction_module.db, "record_capital_reduction_action", new=AsyncMock()):
        blocked = await CapitalReductionEngine(TradeSettings.defaults(), NoActionExecutor()).run_once()
    assert_true(blocked["state"] == "blocked", "temporary lack of a broker-valid reduction action was not reported as blocked")
    assert_true(any(update.get("status") == "active" for update in state_updates), "temporary reduction blockage terminalized the session instead of preserving ACTIVE retry state")


async def test_capital_reduction_closest_action() -> None:
    class CandidateExecutor:
        async def get_symbol_info(self, symbol):
            return {"last_tick_time": time.time(), "tick_size": 0.01, "tick_value": 0.01, "contract_size": 1.0, "min_lot": 1.0, "max_lot": 10.0, "step_lot": 1.0}
        async def get_symbol_price(self, symbol):
            return (100.0, 104.0 if symbol == "near" else 108.0)
        async def get_broker_margin_for_volume(self, symbol, direction, volume, price):
            return {"margin": volume, "margin_source": "fixture"}

    engine = CapitalReductionEngine(TradeSettings.defaults(), CandidateExecutor())
    engine.broker_usable_symbols = ("near", "far")
    plan, reason, diagnostics = await engine._plan_round_trip(
        {"equity": 100.0, "free_margin": 100.0, "leverage": 10.0},
        remaining=4.0, tolerance=0.0, overshoot_tolerance=0.0, initial_required_reduction=4.0,
    )
    assert_true(plan is not None and plan.symbol == "near", "capital reduction did not choose the closest valid broker action")
    assert_true(diagnostics["best_candidate"]["distance_to_target"] == 0.0 and not reason, "closest-action evidence was not persisted in the reduction planner")


def test_capital_reduction_view_is_phase_free() -> None:
    report = capital_test_view(
        account={"currency": "USD"},
        session={
            "id": 9, "status": "active", "runtime_state": "BLOCKED", "target_equity": 500.0,
            "current_equity": 900.0, "remaining": 400.0, "tolerance": 5.0,
            "progress_pct": 20.0, "metadata": {"last_planning": {"valid_candidate_count": 0}},
            "error_reason": "No broker-valid action currently available",
        }, target=None, tolerance=0.0,
    )
    assert_true("CAPITAL REDUCTION" in report and "State: ACTIVE" in report, "reduction view did not preserve an active session during temporary blockage")
    assert_true("phase" not in report.lower() and "objective complete" not in report.lower(), "reduction Telegram view leaked growth-phase or objective-completion language")
    assert_true("Valid broker actions" in report and "Progress:" in report, "reduction Telegram view omitted continuous-session diagnostics")


async def test_broker_authoritative_capital_state() -> None:
    class FakeBroker:
        def __init__(self) -> None:
            self.available = True
            self.balance = 100.0
            self.equity = 100.0
            self.free_margin = 100.0
            self.margin_level = 1_000.0
            self.positions = []

        async def get_live_account_snapshot(self, history_days=0):
            if not self.available:
                return {"current": False, "error": "MT5 unavailable"}
            return {
                "current": True, "retrieved_at": "2026-08-13T10:00:00Z",
                "account": {
                    "login": 123456, "broker_account_mode": "demo", "balance": self.balance,
                    "equity": self.equity, "free_margin": self.free_margin,
                    "margin": max(0.0, self.equity - self.free_margin), "margin_level": self.margin_level, "margin_so_call": 100.0,
                    "margin_so_so": 50.0, "leverage": 100, "currency": "USD",
                },
                "positions": list(self.positions),
            }

        async def get_symbol_execution_metadata(self, symbol, direction="BUY"):
            return {
                "symbol": symbol, "selected": True, "visible": True, "trade_mode": 4, "order_mode": 127,
                "bid": 100.0, "ask": 100.0, "last": 100.0, "point": 0.01, "digits": 2,
                "tick_size": 0.01, "tick_value": 1.0, "volume_min": 0.1, "volume_max": 100.0,
                "volume_step": 0.1, "contract_size": 100.0, "trade_contract_size": 100.0,
                "margin_required": 10.0, "margin_source": "order_calc_margin",
            }

    with tempfile.TemporaryDirectory() as directory:
        path = os.path.join(directory, "capital_state.db")
        await db.init_db(path)
        settings = TradeSettings.defaults()
        settings.enabled_symbols = ["Volatility 75 Index"]
        settings.trading_mode = "demo"
        broker = FakeBroker()
        service = CapitalStateService(settings, broker, db_path=path)
        normal = await service.evaluate()
        assert_true(normal["state"] == AccountCapitalState.NORMAL and normal["demo_session_id"], "normal broker account did not create a valid DEMO session")
        audit = normal.get("broker_metadata") or {}
        first_symbol = (audit.get("symbols") or [{}])[0]
        assert_true(first_symbol.get("usable") and first_symbol.get("checks", {}).get("leverage") == "NOT_EXPOSED", "valid MT5 direct margin evidence incorrectly required symbol-level leverage")
        first_session = normal["demo_session_id"]
        repeat = await service.evaluate()
        assert_true(repeat["demo_session_id"] == first_session and not repeat["changed"], "steady account state created a duplicate DEMO session or event")

        broker.margin_level = 99.0
        critical = await service.evaluate()
        assert_true(critical["state"] == AccountCapitalState.CRITICAL_CAPITAL and critical["state"] in AccountCapitalState.EXPOSURE_BLOCKING and critical["state"] not in AccountCapitalState.BLOCKING, "broker margin-call condition did not enter exposure-only critical protection state")

        broker.margin_level = 1_000.0
        broker.free_margin = 5.0
        exhausted = await service.evaluate()
        exhausted_symbol = ((exhausted.get("broker_metadata") or {}).get("symbols") or [{}])[0]
        assert_true(exhausted_symbol.get("specification_valid") and not exhausted_symbol.get("usable"), "valid symbol specification was lost when only current free margin became insufficient")
        assert_true(exhausted["state"] == AccountCapitalState.MARGIN_PRESSURE, "insufficient broker free margin did not enter non-terminal margin pressure")
        persisted = await db.get_account_state("demo", path)
        assert_true(persisted and persisted["state"] == AccountCapitalState.MARGIN_PRESSURE, "margin-pressure state was not persisted authoritatively")

        broker.balance = broker.equity = 1.0
        broker.free_margin = -0.5
        broker.positions = [{"ticket": 1, "volume": 0.1, "profit": -0.5}]
        pressured_with_position = await service.evaluate()
        assert_true(pressured_with_position["state"] == AccountCapitalState.MARGIN_PRESSURE and pressured_with_position["open_position_count"] == 1, "open positions did not defer terminal exhaustion under margin pressure")

        broker.positions = []
        exhausted_flat = await service.evaluate()
        assert_true(exhausted_flat["state"] == AccountCapitalState.CAPITAL_EXHAUSTED, "flat broker account below the terminal balance threshold was not exhausted")

        broker.balance = broker.equity = broker.free_margin = 1_000.0
        reset = await service.evaluate()
        assert_true(reset["state"] == AccountCapitalState.AWAITING_RESUME and reset["reset_detected"], "broker balance increase after exhaustion did not create a verified reset state")
        resumed = await service.verify_resume()
        assert_true(resumed["resume_verified"] and resumed["state"] == AccountCapitalState.NORMAL, "verified DEMO reset could not enter a resumable normal state")

        broker.available = False
        unknown = await service.evaluate()
        assert_true(unknown["state"] == AccountCapitalState.ACCOUNT_STATE_UNKNOWN, "unavailable MT5 account was not fail-closed")

        broker.available = True
        broker.balance = broker.equity = broker.free_margin = 1_000.0
        service.set_verified_target_universe([], {"broker_symbols_returned": 30, "synthetic_targets_detected": 0, "gold_targets_detected": 0, "broker_verified_targets": 0})
        target_empty = await service.evaluate()
        assert_true(target_empty["state"] == AccountCapitalState.TARGET_UNIVERSE_EMPTY and target_empty["broker_metadata"]["target_count"] == 0, "zero targets were misclassified as invalid symbol metadata")

        service.begin_target_universe_refresh({"broker_symbols_returned": 30})
        initializing = await service.evaluate()
        assert_true(initializing["state"] == AccountCapitalState.TARGET_UNIVERSE_INITIALIZING, "validator did not distinguish an in-progress universe handoff")

        service.set_verified_target_universe(["Volatility 75 Index"], {"broker_symbols_returned": 30, "synthetic_targets_detected": 1, "gold_targets_detected": 0, "broker_verified_targets": 1})
        verified = await service.evaluate()
        assert_true(verified["state"] == AccountCapitalState.ACCOUNT_VERIFIED and verified["broker_metadata"]["target_count"] == 1, "completed broker-universe handoff did not reach account validation")


async def test_sizing_rejection_diagnostic_persistence() -> None:
    with tempfile.TemporaryDirectory() as directory:
        path = os.path.join(directory, "sizing.db")
        await db.init_db(path)
        setup_id = await db.record_setup(
            account_mode="demo", symbol="XAUUSDmicro", timeframe="M5", direction="BUY", setup_type="test",
            status="sizing_rejected", entry_price=100.0, stop_loss=99.0, take_profit=103.0, rr_ratio=3.0,
            db_path=path,
        )
        await db.record_execution_event(
            account_mode="demo", symbol="XAUUSDmicro", setup_id=setup_id, status="sizing_rejected",
            requested_price=100.0, reason="fixture", details={"sizing": {"sizing_code": "MINIMUM_LOT_EXCEEDS_POLICY_RISK"}, "sizing_inputs": {"risk_pct": 1.0, "entry_price": 100.0}}, db_path=path,
        )
        latest = await db.get_latest_sizing_rejection(account_mode="demo", symbol="XAUUSDmicro", db_path=path)
        assert_true(latest is not None and latest["entry_price"] == 100.0, "latest sizing rejection did not retain setup geometry")
        assert_true(latest["details"]["sizing_inputs"]["risk_pct"] == 1.0, "latest sizing rejection lost sizing inputs")


async def test_admin_command_error_reply() -> None:
    class Reply:
        def __init__(self) -> None:
            self.messages: list[str] = []
            self.text = "/objective"

        async def reply_text(self, text: str, **kwargs) -> None:
            self.messages.append(text)

    class UpdateFixture:
        def __init__(self) -> None:
            self.effective_user = SimpleNamespace(id=1)
            self.message = Reply()
            self.callback_query = None

    @admin_only
    async def _broken_command(_self, _update, _context):
        raise RuntimeError("fixture failure")

    update = UpdateFixture()
    # The failure is intentional test data. Suppress its production logger
    # output so VPS smoke validation remains readable while still asserting the
    # user-facing recovery message.
    with patch("bot.handlers.is_admin", return_value=True), patch("bot.handlers.logger.exception"):
        await _broken_command(object(), update, SimpleNamespace())
    assert_true(update.message.messages and "COMMAND ERROR" in update.message.messages[0] and "RuntimeError" in update.message.messages[0], "admin command exceptions still failed silently in Telegram")


async def test_objective_markdown_fallback() -> None:
    class Reply:
        def __init__(self) -> None:
            self.calls: list[tuple[str, dict]] = []

        async def reply_text(self, text: str, **kwargs) -> None:
            self.calls.append((text, kwargs))
            if len(self.calls) == 1:
                raise BadRequest("Can't parse entities")

    reply = Reply()
    with patch("bot.handlers.logger.warning"):
        await BotHandlers._reply_objective(reply, "**OBJECTIVE**\nState: CAPITAL_EXHAUSTED")
    assert_true(len(reply.calls) == 2 and reply.calls[0][1].get("parse_mode") == "Markdown", "Objective Console did not attempt formatted rendering first")
    assert_true(reply.calls[1][1].get("parse_mode") is None and "CAPITALEXHAUSTED" in reply.calls[1][0], "Objective Console did not send a plain-text fallback after Telegram rejected Markdown")


async def test_objective_broker_universe_separation() -> None:
    handler = object.__new__(BotHandlers)
    handler.scheduler = SimpleNamespace(
        last_capital_state={
            "account": {"equity": 42.77, "free_margin": -0.48, "currency": "USD"},
            "state": "CAPITAL_EXHAUSTED",
        },
        _analysis_eligible_symbols=(),
        market_universe=SimpleNamespace(
            accepted_records=[SimpleNamespace(symbol="Boom 500 Index"), SimpleNamespace(symbol="XAUUSDmicro")]
        ),
    )
    handler.settings = SimpleNamespace(trading_mode="demo")
    account, state, approved = await handler._objective_facts(refresh=False)
    assert_true(state == "CAPITAL_EXHAUSTED" and account["free_margin"] < 0, "fixture did not preserve the broker capital block")
    assert_true(approved == ("Boom 500 Index", "XAUUSDmicro"), "Objective Console incorrectly hid broker-approved symbols when margin feasibility was zero")


async def test_objective_console_safety() -> None:
    interpreter = ObjectiveInterpreter()
    account = {"equity": 152.60, "free_margin": 152.60, "currency": "USD"}
    usable = ("Volatility 75 Index", "XAUUSDmicro")
    objective = interpreter.parse(
        "Start with $50 and aim for $10,000 aggressively while protecting capital aggressively. "
        "Trade Synthetic Indices and Gold with RR 0 and adaptive learning.",
        account_mode="demo",
    )
    assert_true(objective.account_mode == "demo", "objective parser changed DEMO mode when the instruction omitted the word demo")
    assert_true(objective.minimum_rr == 0.0, "objective parser did not preserve an explicit zero-RR request")
    validation = ObjectiveValidator.validate(objective, account_snapshot=account, account_state="ACCOUNT_VERIFIED", broker_usable_symbols=usable)
    assert_true(validation.valid and not validation.errors, "valid DEMO objective was incorrectly blocked")
    assert_true(any("not a guaranteed" in warning for warning in validation.warnings), "large growth target was not labelled as non-guaranteed")
    assert_true(phase_for_equity(50.0, 152.60) == "GROWTH", "objective phase calculation is incorrect")
    assert_true(objective_operational_readiness(account, "ACCOUNT_VERIFIED")[0] == "READY", "positive broker capital was not objective-ready")
    exhausted_account = {"equity": 23.67, "free_margin": -10.34, "currency": "USD"}
    exhausted_validation = ObjectiveValidator.validate(objective, account_snapshot=exhausted_account, account_state="CAPITAL_EXHAUSTED", broker_usable_symbols=usable)
    assert_true(not exhausted_validation.valid and any("CAPITAL_EXHAUSTED" in error for error in exhausted_validation.errors), "capital-exhausted account still confirmed an objective")
    assert_true(objective_operational_readiness(exhausted_account, "CAPITAL_EXHAUSTED")[0] == "BLOCKED_CAPITAL", "capital exhaustion was presented as scanner-ready")
    assert_true(objective_operational_readiness({"equity": 50.0, "free_margin": -0.01}, "ACCOUNT_VERIFIED")[0] == "BLOCKED_MARGIN", "negative broker free margin was presented as scanner-ready")

    below_target = interpreter.parse("Start with $100 and aim for $50", account_mode="demo")
    below_validation = ObjectiveValidator.validate(below_target, account_snapshot=account, account_state="ACCOUNT_VERIFIED", broker_usable_symbols=usable)
    assert_true(not below_validation.valid and below_validation.errors, "target below starting capital remained confirmable")

    unsupported = interpreter.parse("Start with $50 and aim for $100 with XAUEUR", account_mode="demo")
    _, unsupported_resolution = resolve_requested_symbols(unsupported.requested_symbols, usable)
    unsupported_validation = ObjectiveValidator.validate(unsupported, account_snapshot=account, account_state="ACCOUNT_VERIFIED", broker_usable_symbols=usable, resolved_symbols=unsupported_resolution)
    assert_true(not unsupported_validation.valid and any("XAUEUR" in error for error in unsupported_validation.errors), "unsupported broker symbol was not blocked")
    assert_true(not hasattr(interpreter, "executor") and not hasattr(interpreter, "mt5"), "objective parser was given an execution boundary")

    requested = interpreter.parse(
        "Start with $50, aim for $10,000 aggressively while protecting capital aggressively. "
        "Trade only Boom 100 Index, Boom 500 Index, Volatility 75 Index and XAUUSDmicro. "
        "Use layering, adaptive sizing, adaptive TP/SL and learning. Fully automate everything.",
        account_mode="demo",
    )
    requested_usable = ("Boom 100 Index", "Boom 500 Index", "Volatility 75 Index", "XAUUSDmicro", "Crash 500 Index")
    resolved, resolution = resolve_requested_symbols(requested.requested_symbols, requested_usable)
    requested_validation = ObjectiveValidator.validate(requested, account_snapshot=account, account_state="ACCOUNT_VERIFIED", broker_usable_symbols=requested_usable, resolved_symbols=resolution)
    assert_true(requested.full_auto and requested_validation.valid, "fully automated DEMO objective was not parsed as confirmable")
    assert_true(resolved == ("Boom 100 Index", "Boom 500 Index", "Volatility 75 Index", "XAUUSDmicro"), "explicit objective instruments were not resolved exactly against broker symbols")
    assert_true("Crash 500 Index" not in resolved and all(row["status"] == "BROKER_VERIFIED" for row in resolution), "objective universe silently expanded or unresolved a requested instrument")

    with tempfile.TemporaryDirectory() as directory:
        path = os.path.join(directory, "objectives.db")
        await db.init_db(path)
        first = await db.create_objective_draft(
            account_mode="demo", raw_instruction=objective.raw_instruction, objective=objective.to_dict(),
            account_snapshot=account, broker_universe=list(usable), context={"phase": "GROWTH", "validation": validation.to_dict()}, db_path=path,
        )
        assert_true(first["status"] == "draft", "objective draft was unexpectedly activated")
        active_one = await db.confirm_objective_draft(
            "demo", objective=objective.to_dict(), account_snapshot=account, broker_universe=list(usable),
            context={"phase": "GROWTH", "validation": validation.to_dict()}, db_path=path,
        )
        assert_true(active_one["status"] == "active" and active_one["version"] == 1, "first confirmed objective was not versioned active")
        await db.create_objective_draft(
            account_mode="demo", raw_instruction="Start with $60 and aim for $120", objective=objective.to_dict(),
            account_snapshot=account, broker_universe=list(usable), context={"phase": "GROWTH", "validation": validation.to_dict()}, db_path=path,
        )
        active_two = await db.confirm_objective_draft("demo", db_path=path)
        assert_true(active_two["version"] == 2, "new confirmed objective did not receive the next immutable version")
        history = await db.list_objective_history("demo", db_path=path)
        assert_true({row["status"] for row in history} == {"active", "superseded"}, "objective history overwrote a prior active version")
        assert_true(await db.set_objective_paused("demo", True, db_path=path), "active objective context did not accept a pause request")
        paused = await db.get_active_objective("demo", db_path=path)
        assert_true(paused and paused["is_paused"], "objective pause state was not persisted")
        session_id = await db.create_demo_session(broker_login="fixture", start_balance=50.0, start_equity=50.0, db_path=path)
        terminal = await db.mark_active_objective_terminal(
            account_mode="demo", outcome="target_reached", terminal_state="OBJECTIVE_TARGET_REACHED",
            demo_session_id=session_id, terminal_equity=100.0, reason="fixture target reached", db_path=path,
        )
        assert_true(terminal and terminal["is_paused"], "terminal objective did not remain paused")
        assert_true((terminal.get("context") or {}).get("operational", {}).get("terminal", {}).get("outcome") == "target_reached", "terminal objective outcome was not persisted")
        assert_true(not await db.set_objective_paused("demo", False, db_path=path), "terminal objective could be resumed without a new confirmation")
        claimed = await db.claim_objective_session_review(
            demo_session_id=session_id, objective_id=terminal["id"], outcome="target_reached",
            terminal_state="OBJECTIVE_TARGET_REACHED", summary={"strategy_trades": 2}, db_path=path,
        )
        assert_true(claimed, "first terminal session review was not claimed")
        assert_true(not await db.claim_objective_session_review(
            demo_session_id=session_id, objective_id=terminal["id"], outcome="target_reached",
            terminal_state="OBJECTIVE_TARGET_REACHED", db_path=path,
        ), "terminal session review was not idempotent")
        await db.complete_objective_session_review(session_id, summary={"strategy_trades": 2}, optimization={"decision": "recorded"}, db_path=path)
        review = await db.get_objective_session_review(session_id, db_path=path)
        assert_true(review and review["optimization"].get("decision") == "recorded", "terminal session review did not retain its evidence result")

    # The scheduler must honor a confirmed explicit objective instead of the
    # normal evidence-governed top-ten cohort. This fixture has no MT5/executor.
    engine = object.__new__(scheduler.MarketScheduler)
    engine.settings = TradeSettings.defaults()
    engine.settings.trading_mode = "demo"
    engine._operational_objective = {}
    active_fixture = {
        "id": 7, "version": 3, "is_paused": False,
        "context": {"operational": {"explicit_symbol_universe": True, "allowed_symbols": list(resolved), "minimum_rr": 0.0, "layering_preference": "enabled", "full_auto": True}},
    }
    original_get_active = db.get_active_objective
    async def _fixture_active_objective(_mode: str):
        return active_fixture
    db.get_active_objective = _fixture_active_objective
    try:
        snapshot = {"market_selection": {"state": "ranked", "selected_symbols": ["Crash 500 Index"], "disabled_symbols": [], "universe_size": len(requested_usable)}}
        scoped = await engine._apply_operational_objective(requested_usable, snapshot)
    finally:
        db.get_active_objective = original_get_active
    assert_true(scoped == list(resolved) and snapshot["market_selection"]["state"] == "objective_explicit_universe", "confirmed objective did not override research cohort with its explicit broker-verified allowlist")
    assert_true(engine._objective_min_rr() == 0.0 and "Crash 500 Index" not in scoped, "objective RR or strict instrument operational settings were not applied")


async def test_objective_phase_lifecycle() -> None:
    plan = plan_objective_phases(
        starting_equity=10.0, target_equity=10_000.0, minimum_operating_capital=2.0,
        historical_evidence={"sample_size": 20, "expectancy_r": 0.4, "max_drawdown_r": 1.0},
    )
    assert_true(plan.phase_targets[-1] == 10_000.0 and len(plan.phase_targets) >= 2, "adaptive phase plan did not preserve the exact final objective target")
    assert_true(all(right > left for left, right in zip((10.0, *plan.phase_targets[:-1]), plan.phase_targets)), "adaptive phase plan produced a non-increasing milestone")
    legacy_evidence_plan = plan_objective_phases(
        starting_equity=48.61, target_equity=10_000.0, minimum_operating_capital=float("nan"),
        historical_evidence={"sample_size": float("nan"), "expectancy_r": float("inf"), "max_drawdown_r": float("-inf")},
    )
    assert_true(legacy_evidence_plan.phase_targets[-1] == 10_000.0 and len(legacy_evidence_plan.phase_targets) >= 2, "non-finite legacy learning evidence prevented adaptive objective phase planning")

    with tempfile.TemporaryDirectory() as directory:
        path = os.path.join(directory, "phases.db")
        await db.init_db(path)
        account = {"equity": 10.0, "free_margin": 10.0, "currency": "USD"}
        objective = TradingObjective(raw_instruction="fixture", account_mode="demo", starting_capital=10.0, target_capital=100.0)
        await db.create_objective_draft(
            account_mode="demo", raw_instruction="fixture", objective=objective.to_dict(), account_snapshot=account,
            broker_universe=["Boom 100 Index"], context={"operational": {}}, db_path=path,
        )
        active = await db.confirm_objective_draft("demo", db_path=path)
        session_id = await db.create_demo_session(broker_login="fixture", start_balance=10.0, start_equity=10.0, db_path=path)
        phases = await db.create_objective_phase_plan(
            objective_id=active["id"], demo_session_id=session_id, starting_equity=10.0,
            phase_targets=[25.0, 50.0, 100.0], policy_snapshot={"model_version": "fixture"},
            instruments=["Boom 100 Index"], db_path=path,
        )
        first = phases[0]
        assert_true(first["status"] == "active" and first["target_equity"] == 25.0, "first objective phase was not activated")
        setup_id = await db.record_setup(
            account_mode="demo", symbol="Boom 100 Index", timeframe="M5", direction="BUY", setup_type="fixture",
            status="executed", objective_phase_id=first["id"], db_path=path,
        )
        trade_id = await db.record_trade(
            symbol="Boom 100 Index", direction="BUY", entry_price=100.0, sl_price=99.0, tp_price=102.0,
            lot_size=0.2, score=80.0, rr_ratio=2.0, executor="fixture", raw_signal="{}", ticket=7,
            setup_id=setup_id, initial_risk=1.0, policy_version="fixture", demo_session_id=session_id,
            objective_phase_id=first["id"], db_path=path,
        )
        await db.close_trade(trade_id, pnl=2.0, pnl_r=2.0, exit_price=102.0, max_favorable_r=2.2, max_adverse_r=-0.2, db_path=path)
        metrics = await db.objective_phase_summary(first["id"], db_path=path)
        assert_true(metrics["setups_traded"] == 1 and metrics["trades_taken"] == 1 and metrics["best_instrument"] == "Boom 100 Index", "phase review did not use phase-attributed trade and setup evidence")
        completed, successor = await db.complete_objective_phase(
            first["id"], ending_equity=25.5, reason="fixture broker target reached", metrics=metrics,
            next_policy_snapshot={"model_version": "fixture_next"}, next_instruments=["Boom 100 Index"], db_path=path,
        )
        assert_true(completed and completed["status"] == "completed" and successor and successor["status"] == "active", "phase completion did not freeze evidence and activate the next milestone")
        assert_true(successor["starting_equity"] == 25.5 and successor["target_equity"] == 50.0, "next phase did not inherit the completed phase ending equity")
        unchanged, duplicate_successor = await db.complete_objective_phase(
            first["id"], ending_equity=30.0, reason="duplicate", metrics={}, db_path=path,
        )
        assert_true(unchanged and unchanged["status"] == "completed" and duplicate_successor is None, "phase completion was not idempotent")
        failed = await db.fail_objective_phase(successor["id"], ending_equity=3.0, reason="CAPITAL_EXHAUSTED", metrics=metrics, db_path=path)
        assert_true(failed and failed["status"] == "failed" and failed["completion_reason"] == "CAPITAL_EXHAUSTED", "verified phase failure was not preserved")


async def test_persistent_objective_template_sessions() -> None:
    with tempfile.TemporaryDirectory() as directory:
        path = os.path.join(directory, "objective_sessions.db")
        await db.init_db(path)
        objective = TradingObjective(raw_instruction="saved fixture", account_mode="demo", starting_capital=10.0, target_capital=100.0)
        await db.create_objective_draft(
            account_mode="demo", raw_instruction="saved fixture", objective=objective.to_dict(),
            account_snapshot={"equity": 10.0, "free_margin": 10.0}, broker_universe=["Boom 100 Index"],
            context={"operational": {"allowed_symbols": ["Boom 100 Index"], "template_saved": True}}, db_path=path,
        )
        template = await db.confirm_objective_draft("demo", db_path=path)
        assert_true(template and template["objective"]["target_capital"] == 100.0, "confirmed objective template was not durable")
        first_id = await db.create_demo_session(
            broker_login="fixture", start_balance=10.0, start_equity=10.0,
            objective_id=template["id"], objective_version=template["version"], db_path=path,
        )
        first_plan = await db.create_objective_phase_plan(
            objective_id=template["id"], demo_session_id=first_id, starting_equity=10.0,
            phase_targets=[25.0, 100.0], policy_snapshot={}, instruments=["Boom 100 Index"], db_path=path,
        )
        await db.fail_objective_phase(first_plan[0]["id"], ending_equity=4.0, reason="fixture exhausted", metrics={}, db_path=path)
        await db.close_demo_session(first_id, status="exhausted", balance=4.0, equity=4.0, db_path=path)
        second_id = await db.create_demo_session(
            broker_login="fixture", start_balance=50.0, start_equity=50.0,
            objective_id=template["id"], objective_version=template["version"], db_path=path,
        )
        second_plan_all = await db.create_objective_phase_plan(
            objective_id=template["id"], demo_session_id=second_id, starting_equity=50.0,
            phase_targets=[75.0, 100.0], policy_snapshot={}, instruments=["Boom 100 Index"], db_path=path,
        )
        second_first = second_plan_all[-2]
        assert_true(second_first["session_phase_number"] == 1 and second_first["starting_equity"] == 50.0, "new saved-objective session did not use fresh equity and local phase one")
        assert_true(second_first["target_equity"] == 75.0 and objective.target_capital == 100.0, "saved objective target changed during reset-separated session creation")
        sessions = await db.get_objective_sessions(template["id"], db_path=path)
        assert_true(len(sessions) == 2 and sessions[-1]["start_equity"] == 10.0 and sessions[0]["start_equity"] == 50.0, "prior session history was not immutable across a new session")


def test_session_local_phase_display_number() -> None:
    assert_true(scheduler.MarketScheduler._phase_display_number({"phase_number": 19, "session_phase_number": 1}) == 1, "phase display still uses the cumulative global phase number")
    assert_true(scheduler.MarketScheduler._phase_display_number({"phase_number": 20, "session_phase_number": 2}) == 2, "session-local successor phase number was not selected")
    assert_true(scheduler.MarketScheduler._phase_display_number({"phase_number": 21, "session_phase_number": 0}) == 0, "Phase 0 was coerced to another phase number")
    merged = scheduler.MarketScheduler._apply_phase_management_policy(
        {"breakeven_trigger_r": 1.0, "protection_response": 0.5},
        {"session_phase_number": 0, "policy_snapshot": {"policy": {"breakeven_trigger_r": 0.5, "protection_response": 0.9}}},
    )
    assert_true(merged["active_phase_number"] == 0 and merged["active_phase_role"] == "RECOVERY", "Phase 0 management context was not propagated")
    assert_true(merged["breakeven_trigger_r"] == 0.5 and merged["protection_response"] == 0.9, "current phase management policy did not overlay active-trade SL inputs")


async def test_phase_zero_recovery_lifecycle() -> None:
    with tempfile.TemporaryDirectory() as directory:
        path = os.path.join(directory, "phase_zero.db")
        await db.init_db(path)
        objective = TradingObjective(raw_instruction="phase-zero", account_mode="demo", starting_capital=50.0, target_capital=200.0)
        await db.create_objective_draft(
            account_mode="demo", raw_instruction="phase-zero", objective=objective.to_dict(),
            account_snapshot={"equity": 50.0, "balance": 50.0}, broker_universe=["Boom 100 Index"],
            context={"operational": {}}, db_path=path,
        )
        active = await db.confirm_objective_draft("demo", db_path=path)
        session_id = await db.create_demo_session(
            broker_login="fixture", start_balance=50.0, start_equity=50.0,
            objective_id=active["id"], objective_version=active["version"], db_path=path,
        )
        phases = await db.create_objective_phase_plan(
            objective_id=active["id"], demo_session_id=session_id, starting_equity=50.0,
            phase_targets=[75.0, 200.0], policy_snapshot={"policy": {"breakeven_trigger_r": 1.0}},
            instruments=["Boom 100 Index"], include_recovery_phase=True, db_path=path,
        )
        phase_zero = next(item for item in phases if item["session_phase_number"] == 0)
        phase_one = next(item for item in phases if item["session_phase_number"] == 1)
        assert_true(phase_zero["status"] == "planned" and phase_one["status"] == "active", "new session did not start at active Phase 1 with planned Phase 0")
        recovery = await db.activate_objective_recovery_phase(
            active["id"], demo_session_id=session_id, recovery_equity=40.0, recovery_target_equity=50.0,
            policy_snapshot={"phase_role": "RECOVERY", "policy": {"breakeven_trigger_r": 0.5}},
            instruments=["Boom 100 Index"], reason="fixture loss", db_path=path,
        )
        assert_true(recovery and recovery["session_phase_number"] == 0 and recovery["status"] == "active", "Phase 0 was not activated below session starting balance")
        paused_phase_one = await db.get_objective_phase(phase_one["id"], db_path=path)
        assert_true(paused_phase_one["status"] == "recovery_paused", "growth phase was not paused while Phase 0 was active")
        completed, successor = await db.complete_objective_recovery_phase(
            recovery["id"], ending_equity=50.0, reason="fixture recovery", metrics={},
            next_policy_snapshot={"policy": {"breakeven_trigger_r": 1.0}}, next_instruments=["Boom 100 Index"], db_path=path,
        )
        assert_true(completed["status"] == "completed" and successor["session_phase_number"] == 1 and successor["status"] == "active", "Phase 0 did not return control to active Phase 1")
        assert_true(float(successor["starting_equity"]) == 50.0, "Phase 1 did not restart from the recovered session starting balance")


async def test_objective_scope_disable_persistence() -> None:
    with tempfile.TemporaryDirectory() as directory:
        path = os.path.join(directory, "objective_scope.db")
        await db.init_db(path)
        await db.create_objective_draft(
            account_mode="demo", raw_instruction="standalone scope fixture",
            objective={"account_mode": "demo", "starting_capital": 50.0, "target_capital": 100.0},
            account_snapshot={"equity": 50.0, "state": "ACCOUNT_VERIFIED"},
            broker_universe=["Boom 100 Index"],
            context={"operational": {"allowed_symbols": ["Boom 100 Index"], "explicit_symbol_universe": True}},
            db_path=path,
        )
        active = await db.confirm_objective_draft("demo", db_path=path)
        assert_true(active and active["status"] == "active", "objective fixture was not activated")
        changed = await db.set_objective_scope_disabled("demo", True, db_path=path)
        disabled = await db.get_active_objective("demo", db_path=path)
        assert_true(changed and disabled and disabled["status"] == "active", "disabling scope removed the saved objective")
        assert_true(bool((disabled.get("context") or {}).get("operational", {}).get("scope_disabled")), "disabled objective scope was not persisted")
        changed = await db.set_objective_scope_disabled("demo", False, db_path=path)
        enabled = await db.get_active_objective("demo", db_path=path)
        assert_true(changed and not bool((enabled.get("context") or {}).get("operational", {}).get("scope_disabled")), "objective scope was not re-enabled")


async def test_sl_protection_requires_broker_confirmation() -> None:
    before = Position(ticket=8736588628, symbol="Volatility 10 Index", direction="BUY", volume=0.2, entry_price=4860.0, sl=4857.873, tp=4900.0, profit=2.0)

    class ModifyExecutor:
        def __init__(self, apply_change: bool):
            self.apply_change = apply_change
            self.position = before

        async def get_open_positions(self):
            return [self.position]

    engine = object.__new__(scheduler.MarketScheduler)
    engine.executor = ModifyExecutor(False)
    live, reason = await engine._confirm_position_sl(before, 4867.68854, attempts=2)
    assert_true(live is None and "does not protect profit" in reason, "unchanged broker SL was incorrectly treated as confirmed protection")

    class ApplyingExecutor(ModifyExecutor):
        async def get_open_positions(self):
            return [self.position]

    engine.executor = ApplyingExecutor(True)
    engine.executor.position = Position(ticket=before.ticket, symbol=before.symbol, direction=before.direction, volume=before.volume, entry_price=before.entry_price, sl=4867.68854, tp=before.tp, profit=before.profit)
    live, reason = await engine._confirm_position_sl(before, 4867.68854, attempts=1)
    assert_true(live is not None and float(live.sl) == 4867.68854 and reason == "broker-confirmed", "broker-refreshed improved SL was not accepted as confirmed protection")


async def test_phase_boundary_closes_unprotected_position() -> None:
    class BoundaryExecutor:
        def __init__(self):
            self.positions = [
                Position(ticket=91, symbol="Boom 100 Index", direction="BUY", volume=0.2, entry_price=100.0, sl=99.0, tp=103.0, profit=-1.0),
                Position(ticket=92, symbol="Boom 500 Index", direction="BUY", volume=0.2, entry_price=100.0, sl=100.5, tp=103.0, profit=1.0),
            ]
            self.closed = []

        async def get_open_positions(self):
            return list(self.positions)

        async def close_position(self, ticket):
            self.closed.append(int(ticket))
            self.positions = [position for position in self.positions if int(position.ticket) != int(ticket)]
            return True

        async def modify_position(self, ticket, sl=None, tp=None):
            return False

    engine = object.__new__(scheduler.MarketScheduler)
    engine.settings = TradeSettings.defaults()
    engine.settings.trading_mode = "demo"
    engine.executor = BoundaryExecutor()
    engine._position_management_lock = asyncio.Lock()
    engine.telemetry = RuntimeTelemetry()
    engine.last_capital_state = {"account": {"equity": 100.0, "balance": 100.0, "free_margin": 100.0}}
    engine._management_protection_context = AsyncMock(return_value={})
    original_basket = db.get_basket_for_ticket
    original_logs = db.log_trade_action
    original_baskets = db.get_open_baskets
    original_flat = db.close_basket_if_flat
    async def _no_basket(*args, **kwargs):
        return None
    async def _no_log(*args, **kwargs):
        return None
    async def _no_baskets(*args, **kwargs):
        return []
    async def _no_flat(*args, **kwargs):
        return True
    db.get_basket_for_ticket = _no_basket
    db.log_trade_action = _no_log
    db.get_open_baskets = _no_baskets
    db.close_basket_if_flat = _no_flat
    try:
        result = await engine._phase_boundary_protect_positions(phase={"id": 7, "session_phase_number": 1, "phase_number": 19})
    finally:
        db.get_basket_for_ticket = original_basket
        db.log_trade_action = original_logs
        db.get_open_baskets = original_baskets
        db.close_basket_if_flat = original_flat
    assert_true(result["attempted"] == 2 and result["closed"] == 0 and result["unchanged"] == 1 and result["protected"] == 1 and result["pending"] == 0 and result["failed"] == 0, "phase boundary did not preserve positions while recording protection state")
    assert_true(engine.executor.closed == [] and [position.ticket for position in engine.executor.positions] == [91, 92], "phase boundary closed a position merely because the phase completed")


async def test_phase_boundary_preserves_unprotected_profit() -> None:
    class BoundaryExecutor:
        def __init__(self):
            self.positions = [Position(ticket=93, symbol="Boom 100 Index", direction="BUY", volume=0.2, entry_price=100.0, sl=99.0, tp=103.0, profit=1.0)]
            self.closed = []

        async def get_open_positions(self):
            return list(self.positions)

        async def close_position(self, ticket):
            self.closed.append(int(ticket))
            self.positions = [position for position in self.positions if int(position.ticket) != int(ticket)]
            return True

        async def modify_position(self, ticket, sl=None, tp=None):
            return False

    engine = object.__new__(scheduler.MarketScheduler)
    engine.settings = TradeSettings.defaults()
    engine.settings.trading_mode = "demo"
    engine.executor = BoundaryExecutor()
    engine._position_management_lock = asyncio.Lock()
    engine.optimizer = SimpleNamespace(active_policy=AsyncMock(return_value=(SimpleNamespace(to_dict=lambda: {}), None, None)))
    engine.fetch_candles = AsyncMock(return_value=pd.DataFrame())
    engine.telemetry = RuntimeTelemetry()
    engine.last_capital_state = {"account": {"equity": 100.0, "balance": 100.0, "free_margin": 100.0}}
    engine._management_protection_context = AsyncMock(return_value={})
    original_basket = db.get_basket_for_ticket
    original_logs = db.log_trade_action
    original_baskets = db.get_open_baskets
    original_flat = db.close_basket_if_flat
    async def _no_basket(*args, **kwargs):
        return None
    async def _no_log(*args, **kwargs):
        return None
    async def _no_baskets(*args, **kwargs):
        return []
    async def _no_flat(*args, **kwargs):
        return True
    db.get_basket_for_ticket = _no_basket
    db.log_trade_action = _no_log
    db.get_open_baskets = _no_baskets
    db.close_basket_if_flat = _no_flat
    try:
        result = await engine._phase_boundary_protect_positions(phase={"id": 8, "session_phase_number": 1, "phase_number": 20})
    finally:
        db.get_basket_for_ticket = original_basket
        db.log_trade_action = original_logs
        db.get_open_baskets = original_baskets
        db.close_basket_if_flat = original_flat
    assert_true(result["attempted"] == 1 and result["closed"] == 0 and result["protected"] == 0 and result["pending"] == 1 and result["failed"] == 0, "unprotected profitable position did not leave the phase boundary pending")
    assert_true(engine.executor.closed == [] and [position.ticket for position in engine.executor.positions] == [93], "phase boundary closed a profitable position merely because protection was unconfirmed")


async def test_legacy_objective_phase_migration() -> None:
    with tempfile.TemporaryDirectory() as directory:
        path = os.path.join(directory, "legacy_objective.db")
        await db.init_db(path)
        account = {"equity": 50.0, "free_margin": 50.0, "currency": "USD"}
        objective = TradingObjective(raw_instruction="legacy", account_mode="demo", starting_capital=50.0, target_capital=500.0)
        await db.create_objective_draft(
            account_mode="demo", raw_instruction="legacy", objective=objective.to_dict(), account_snapshot=account,
            broker_universe=["Boom 100 Index"], context={"operational": {"allowed_symbols": ["Boom 100 Index"], "full_auto": True}}, db_path=path,
        )
        active = await db.confirm_objective_draft("demo", db_path=path)
        engine = object.__new__(scheduler.MarketScheduler)
        engine.settings = TradeSettings.defaults()
        engine.settings.market_ranking_lookback_days = 30
        engine.last_capital_state = {"account": account, "demo_session_id": None, "minimum_operating_capital": 1.0}
        async def _notify(_message: str, *args, **kwargs):
            return None
        engine._notify = _notify
        original_summary = db.get_management_learning_summary
        original_create = db.create_objective_phase_plan
        original_update = db.update_active_objective_context
        async def _summary(**kwargs):
            return await original_summary(db_path=path, **kwargs)
        async def _create(**kwargs):
            return await original_create(db_path=path, **kwargs)
        async def _update(objective_id, context):
            return await original_update(objective_id, context, db_path=path)
        db.get_management_learning_summary = _summary
        db.create_objective_phase_plan = _create
        db.update_active_objective_context = _update
        try:
            migrated = await engine._ensure_objective_phase_plan(active)
        finally:
            db.get_management_learning_summary = original_summary
            db.create_objective_phase_plan = original_create
            db.update_active_objective_context = original_update
        phases = await db.list_objective_phases(active["id"], db_path=path)
        phase_data = (migrated.get("context") or {}).get("operational") or {}
        assert_true(any(item.get("session_phase_number") == 0 and item.get("status") == "planned" for item in phases), "legacy confirmed objective did not receive a planned Phase 0 recovery row")
        assert_true(any(item.get("session_phase_number") == 1 and item.get("status") == "active" for item in phases), "legacy confirmed objective did not receive an active growth Phase 1")
        assert_true(phase_data.get("phase_plan", {}).get("phase_targets", [])[-1] == 500.0, "legacy phase migration changed the objective target")
        assert_true(phase_data.get("allowed_symbols") == ["Boom 100 Index"], "legacy phase migration changed the broker-resolved objective allowlist")


def test_causal_replay_safety() -> None:
    def make_engine(policy: ExperimentalPolicy | None = None) -> BacktestEngine:
        return BacktestEngine(settings=TradeSettings.defaults(), policy=policy or ExperimentalPolicy(
            breakeven_model="none", trailing_model="none", partial_exit_model="none", exit_on_opposing_structure=False,
        ))

    def replay_trade(direction: str, sl: float, tp: float) -> BacktestTrade:
        return BacktestTrade(
            entry_time=pd.Timestamp("2026-01-01T00:00:00Z"), symbol="TEST", direction=direction,
            entry_price=100.0, stop_loss=sl, initial_stop=sl, take_profit=tp, initial_target=tp,
        )

    one_bar = lambda high, low, close=100.0: pd.DataFrame([{
        "time": pd.Timestamp("2026-01-01T00:01:00Z"), "open": 100.0,
        "high": high, "low": low, "close": close, "volume": 1.0,
    }])

    buy_stop = make_engine(); buy_stop.open_trade = replay_trade("BUY", 98.0, 102.0)
    buy_stop.replay_management_bar(one_bar(101.0, 97.5), 0, 0.01)
    assert_true(buy_stop.trades[-1].exit_reason == "stop_loss", "BUY stop-loss replay did not close at the protective stop")

    buy_target = make_engine(); buy_target.open_trade = replay_trade("BUY", 98.0, 102.0)
    buy_target.replay_management_bar(one_bar(102.5, 99.0), 0, 0.01)
    assert_true(buy_target.trades[-1].exit_reason == "take_profit", "BUY take-profit replay did not close at target")

    sell_stop = make_engine(); sell_stop.open_trade = replay_trade("SELL", 102.0, 98.0)
    sell_stop.replay_management_bar(one_bar(102.5, 99.0), 0, 0.01)
    assert_true(sell_stop.trades[-1].exit_reason == "stop_loss", "SELL stop-loss replay did not close at the protective stop")

    sell_target = make_engine(); sell_target.open_trade = replay_trade("SELL", 102.0, 98.0)
    sell_target.replay_management_bar(one_bar(101.0, 97.5), 0, 0.01)
    assert_true(sell_target.trades[-1].exit_reason == "take_profit", "SELL take-profit replay did not close at target")

    ambiguous = make_engine(); ambiguous.open_trade = replay_trade("BUY", 98.0, 102.0)
    ambiguous.replay_management_bar(one_bar(102.5, 97.5), 0, 0.01)
    assert_true(ambiguous.trades[-1].exit_reason == "stop_loss", "same-candle SL/TP ambiguity was not resolved conservatively as stop first")

    excursions = make_engine(); excursions.open_trade = replay_trade("BUY", 95.0, 105.0)
    excursions.replay_management_bar(one_bar(102.0, 99.0, 100.5), 0, 0.01)
    assert_true(abs(excursions.open_trade.max_favorable_r - 0.4) < 1e-9 and abs(excursions.open_trade.max_adverse_r + 0.2) < 1e-9, "replay MAE/MFE did not use the current candle only")

    management_policy = ExperimentalPolicy(
        breakeven_model="rr", breakeven_trigger_r=0.5, trailing_model="none", partial_exit_model="none", exit_on_opposing_structure=False,
    )
    management = make_engine(management_policy)
    management.open_trade = replay_trade("BUY", 98.0, 110.0)
    management.open_trade.experimental_policy = management_policy.to_dict()
    history = pd.DataFrame([
        {"time": pd.Timestamp("2026-01-01T00:00:00Z") + pd.to_timedelta(index, unit="min"), "open": 100.0, "high": 100.1, "low": 99.9, "close": 100.0, "volume": 1.0}
        for index in range(29)
    ] + [{"time": pd.Timestamp("2026-01-01T00:29:00Z"), "open": 100.0, "high": 101.1, "low": 99.2, "close": 101.0, "volume": 1.0}])
    management.replay_management_bar(history, len(history) - 1, 0.01)
    assert_true(management.open_trade.sl_modifications == 1 and management.open_trade.breakeven_activated, "replay management action was not recorded from the existing TradeManager")

    bars = pd.DataFrame([
        {"time": pd.Timestamp("2026-01-02T00:00:00Z") + pd.to_timedelta(index, unit="min"), "open": 100.0 + index * 0.01, "high": 100.1 + index * 0.01, "low": 99.9 + index * 0.01, "close": 100.0 + index * 0.01, "volume": 1.0}
        for index in range(55)
    ])
    causal = make_engine(); result = causal.run(bars, [], "TEST", "M5")
    assert_true(result.replay_audit, "causal replay did not record any visible-candle audit events")
    assert_true(all(event.visible_bars == event.bar_index + 1 and event.withheld_future_bars == len(bars) - event.visible_bars for event in result.replay_audit), "causal replay audit shows future candles entering an analysis decision")
    assert_true(not any(hasattr(causal, name) for name in ("executor", "data_provider", "broker")), "historical replay unexpectedly owns a network or execution dependency")


async def test_adaptive_management_learning_evidence() -> None:
    with tempfile.TemporaryDirectory() as directory:
        path = os.path.join(directory, "management_learning.db")
        await db.init_db(path)
        setup_id = await db.record_setup(
            account_mode="demo", symbol="Volatility 75 Index", timeframe="M5", direction="BUY",
            setup_type="Liquidity Sweep Reversal", status="executed", entry_price=100.0,
            stop_loss=98.0, take_profit=106.0, rr_ratio=3.0, db_path=path,
        )
        trade_id = await db.record_trade(
            "Volatility 75 Index", "BUY", 100.0, 98.0, 106.0, 0.1, 80.0, 3.0, "mt5", "{}",
            account_mode="demo", ticket=8123, setup_id=setup_id, initial_risk=10.0,
            policy_version="model_v001", db_path=path,
        )
        await db.log_trade_action(8123, "SL Protected", "Policy breakeven at 1.00R", trade_id=trade_id, db_path=path)
        await db.log_trade_action(8123, "TP Extended", "Policy structural trail", trade_id=trade_id, db_path=path)
        await db.log_trade_action(8123, "Partial Take Profit", "policy partial exit", trade_id=trade_id, db_path=path)
        await db.close_trade(
            trade_id, 15.0, path, exit_price=103.0, exit_reason="take_profit",
            pnl_r=1.5, max_favorable_r=2.2, max_adverse_r=-0.4,
        )
        rows = await db.get_management_learning_observations(account_mode="demo", db_path=path)
        assert_true(len(rows) == 1 and rows[0]["exit_reason"] == "take_profit", "adaptive-management query lost broker-confirmed exit reason")
        observation = observation_from_broker_trade(rows[0], rows[0]["management_actions"])
        assert_true(observation.mae_r == -0.4 and observation.mfe_r == 2.2 and observation.final_r == 1.5, "adaptive-management observation lost MAE/MFE/final-R evidence")
        assert_true(observation.sl_modifications == 1 and observation.tp_modifications == 1 and observation.partial_exit_used, "TP/SL management-action evidence was not retained")
        summary = await db.get_management_learning_summary(account_mode="demo", db_path=path)
        assert_true(summary["sample_size"] == 1 and summary["average_mae_r"] == -0.4 and summary["average_mfe_r"] == 2.2, "adaptive-management summary did not use persisted closed outcomes")
        assert_true(summary["basis"] == "broker_confirmed_or_causal_replay_observations", "adaptive-management summary basis is not transparent")

    replay_trade = BacktestTrade(
        entry_time=pd.Timestamp("2026-01-01T00:00:00Z"), exit_time=pd.Timestamp("2026-01-01T00:15:00Z"),
        symbol="Volatility 75 Index", direction="BUY", entry_price=100.0, initial_stop=98.0,
        initial_target=106.0, rr_ratio=3.0, rr_result=1.25, max_favorable_r=2.0, max_adverse_r=-0.5,
        sl_modifications=2, tp_modifications=1, breakeven_activated=True, trailing_activated=True,
        partial_closed=True, exit_reason="take_profit", management_events=[{"action": "move_sl"}],
    )
    replay_result = BacktestResult(symbol="Volatility 75 Index", timeframe="M5", trades=[replay_trade])
    replay_observations = observations_from_backtest(replay_result)
    assert_true(len(replay_observations) == 1 and replay_observations[0].duration_seconds == 900, "causal replay duration was not converted into an adaptive-management observation")
    assert_true(replay_observations[0].mae_r == -0.5 and replay_observations[0].mfe_r == 2.0 and replay_observations[0].sl_modifications == 2, "causal replay TP/SL telemetry was lost")


def test_research_governance_rankings() -> None:
    settings = TradeSettings.defaults()
    settings.research_market_limit = 10
    settings.market_ranking_min_sample_size = 2
    settings.strategy_ranking_limit = 3
    settings.strategy_ranking_min_sample_size = 2
    governance = ResearchGovernance(settings)
    universe = [f"Synthetic {index:02d}" for index in range(1, 13)]
    outcomes = [
        {"symbol": "Synthetic 01", "pnl_r": 1.2}, {"symbol": "Synthetic 01", "pnl_r": 0.8},
        {"symbol": "Synthetic 02", "pnl_r": 0.9}, {"symbol": "Synthetic 02", "pnl_r": 0.5},
        {"symbol": "Synthetic 03", "pnl_r": -1.0}, {"symbol": "Synthetic 03", "pnl_r": -0.4},
    ]
    strong = PolicyEvaluator.evaluate([{"pnl_r": 1.2}, {"pnl_r": 0.8}]).to_dict()
    weaker = PolicyEvaluator.evaluate([{"pnl_r": 0.9}, {"pnl_r": 0.5}]).to_dict()
    negative = PolicyEvaluator.evaluate([{"pnl_r": -1.0}, {"pnl_r": -0.4}]).to_dict()
    models = [
        {"version": "model_v001", "role": "champion", "status": "active", "parameters": {}, "performance": {"forward_demo": strong}},
        {"version": "model_v002", "role": "challenger", "status": "evaluated", "parameters": {}, "performance": {"out_of_sample": weaker}},
        {"version": "model_v003", "role": "challenger", "status": "rejected", "parameters": {}, "performance": {"out_of_sample": negative}},
    ]
    snapshot = governance.governance_snapshot(universe, outcomes, models)
    markets = snapshot["market_selection"]
    assert_true(len(markets["selected_symbols"]) == 10, "research governance did not bound the execution cohort to ten broker-valid markets")
    assert_true("Synthetic 01" in markets["selected_symbols"] and "Synthetic 02" in markets["selected_symbols"], "positive evidence markets were not prioritized")
    assert_true(len(markets["disabled_symbols"]) == 2 and set(markets["disabled_symbols"]).isdisjoint(markets["selected_symbols"]), "non-selected broker markets were not explicitly disabled")
    strategies = snapshot["top_strategies"]
    assert_true([row["version"] for row in strategies][:2] == ["model_v001", "model_v002"], "forward-DEMO evidence did not outrank out-of-sample evidence")
    assert_true(snapshot["anti_revenge"]["loss_streak_is_not_a_sizing_input"], "loss streak was permitted to influence position sizing")
    assert_true(snapshot["anti_revenge"]["loss_streak_is_not_a_policy_selection_input"], "loss streak was permitted to select a policy")


async def test_same_day_governance_deferral() -> None:
    engine = object.__new__(scheduler.MarketScheduler)
    engine.settings = TradeSettings.defaults()
    engine.settings.trading_mode = "demo"
    datetime_module = __import__("datetime")
    engine.settings.last_optimization_date = datetime_module.datetime.now(datetime_module.UTC).date().isoformat()
    engine.telemetry = RuntimeTelemetry()
    result = await engine.run_self_optimization()
    assert_true(result["decision"] == "deferred_daily_governance", "same-day governance was not deferred")
    assert_true("cannot trigger" in result["reason"], "anti-revenge deferral reason was not explicit")


async def test_demo_live_partitioning() -> None:
    with tempfile.TemporaryDirectory() as directory:
        path = os.path.join(directory, "modes.db")
        await db.init_db(path)
        demo_trade = await db.record_trade("Volatility 75 Index", "BUY", 100.0, 99.0, 103.0, 0.1, 80.0, 3.0, "mt5", "{}", account_mode="demo", db_path=path)
        live_trade = await db.record_trade("DERIV-GOLD", "SELL", 200.0, 202.0, 194.0, 0.1, 80.0, 3.0, "mt5", "{}", account_mode="live", db_path=path)
        await db.close_trade(demo_trade, 25.0, path)
        await db.close_trade(live_trade, -10.0, path)
        demo = await db.get_performance_summary("demo", db_path=path)
        live = await db.get_performance_summary("live", db_path=path)
        assert_true(demo["trades"] == 1 and demo["pnl"] == 25.0, "demo performance included non-demo results")
        assert_true(live["trades"] == 1 and live["pnl"] == -10.0, "live performance included non-live results")

    migrated = TradeSettings.from_dict({"auto_trade": "false", "trading_mode": "live"})
    assert_true(migrated.trading_mode == "demo", "unconfirmed legacy LIVE mode was not reset to DEMO")
    assert_true(migrated.auto_trade, "legacy installation was not migrated to autonomous demo learning")


def test_explicit_feature_availability() -> None:
    times = pd.date_range("2026-01-01", periods=7, freq="min")
    highs = [1.0, 2.0, 3.0, 10.0, 4.0, 3.0, 2.0]
    lows = [0.0] * len(highs)
    closes = [1.0, 2.0, 3.0, 9.0, 4.0, 3.0, 2.0]
    frame = pd.DataFrame({"time": times, "open": closes, "high": highs, "low": lows, "close": closes})
    structure = analyze_structure(frame, lookback=3)
    assert_true(structure.swing_highs and structure.swing_highs[0].available_index == 6, "swing confirmation availability was not recorded at the final required right-side candle")
    pools = build_liquidity_pools(frame, structure.swing_highs, structure.swing_lows, "M1")
    assert_true(pools and pools[0].created_index >= structure.swing_highs[0].available_index, "liquidity pool became available before its confirmed swing")
    inside = pd.DataFrame({
        "time": pd.date_range("2026-01-02", periods=3, freq="min"),
        "open": [100.0, 101.0, 102.0], "high": [110.0, 108.0, 112.0],
        "low": [90.0, 92.0, 101.0], "close": [102.0, 103.0, 111.0],
    })
    confirmation = detect_inside_bar_breakout(inside, "BUY")
    assert_true(confirmation.confirmed and confirmation.available_index == 2, "inside-bar breakout did not record its closed breakout candle as availability time")


def test_completed_outcome_distribution_statistics() -> None:
    stats = completed_outcome_statistics([
        {"pnl_r": -1.0, "mae_r": -1.1, "mfe_r": 0.2, "target_r": 2.0},
        {"pnl_r": 0.5, "mae_r": -0.4, "mfe_r": 2.2, "target_r": 2.0},
        {"pnl_r": 1.5, "mae_r": -0.2, "mfe_r": 3.1, "target_r": 2.0},
    ])
    assert_true(stats["sample_size"] == 3 and stats["pnl_stddev_r"] is not None, "outcome distribution did not retain sample dispersion")
    assert_true(stats["expectancy_ci95_low_r"] is not None and stats["expectancy_ci95_high_r"] is not None, "outcome uncertainty interval was not produced for a multi-trade sample")
    assert_true(abs(stats["target_reach_probability"] - (2 / 3)) < 1e-9, "target reach probability was not derived from completed MFE observations")
    assert_true(stats["mae_p90_r"] <= 0 and stats["mfe_p90_r"] >= stats["mfe_p50_r"], "MAE/MFE distribution quantiles are inconsistent")


async def test_strategy_transition_evidence_persistence() -> None:
    with tempfile.TemporaryDirectory() as directory:
        path = os.path.join(directory, "transition_evidence.db")
        await db.init_db(path)
        setup_id = await db.record_setup(
            account_mode="demo", symbol="Boom 500 Index", timeframe="M5", direction="SELL",
            setup_type="liquidity_sweep_reversal", status="executed", entry_price=100.0,
            stop_loss=101.0, take_profit=97.0, rr_ratio=3.0,
            strategy_id="liquidity_sweep_reversal", regime="TRENDING",
            previous_regime="RANGING", regime_transition="RANGING->TRENDING", db_path=path,
        )
        trade_id = await db.record_trade(
            "Boom 500 Index", "SELL", 100.0, 101.0, 97.0, 0.1, 10.0, 3.0,
            "mt5", "{}", setup_id=setup_id, account_mode="demo", db_path=path,
        )
        await db.close_trade(trade_id, 8.0, path, pnl_r=0.8, max_favorable_r=1.3, max_adverse_r=-0.25)
        transition = await db.upsert_strategy_transition_evidence(
            "demo", "Boom 500 Index", "liquidity_sweep_reversal", "RANGING", "TRENDING", "M5", path,
        )
        assert_true(transition["sample_size"] == 1 and transition["regime_transition"] == "RANGING->TRENDING", "transition evidence did not retain the originating prior/current regime context")
        repeated = await db.upsert_strategy_transition_evidence(
            "demo", "Boom 500 Index", "liquidity_sweep_reversal", "RANGING", "TRENDING", "M5", path,
        )
        assert_true(repeated["sample_size"] == 1, "transition evidence rebuilt the same closed trade more than once")


def run() -> None:
    test_two_gate_decision_architecture()
    test_broker_stop_normalization()
    asyncio.run(test_engine_scanner_gate_rendering())
    test_pause_resume_command_registration()
    test_pause_recovery_policy()
    asyncio.run(test_immediate_scan_result_reporting())
    asyncio.run(test_capital_reduction_closest_action())
    test_capital_reduction_view_is_phase_free()
    test_scanner_gate_telemetry()
    test_runtime_telemetry()
    test_scan_disposition_truthfulness()
    asyncio.run(test_candle_purpose_separation())
    test_opportunity_context_and_ranking()
    test_capacity_aware_opportunity_selection()
    test_strategy_registry_and_selection()
    test_full_precision_rr_validation()
    asyncio.run(test_single_flight_scan_guard())
    test_scanner_eligibility_handoff()
    test_config_round_trip()
    test_account_monitor_aggregates()
    test_risk_sizing_and_layers()
    test_no_widening_management()
    test_adaptive_capital_protection()
    test_causal_confirmation_invariants()
    test_explicit_feature_availability()
    test_completed_outcome_distribution_statistics()
    asyncio.run(test_broker_only_data_provider())
    asyncio.run(test_deriv_market_universe())
    asyncio.run(test_basket_persistence())
    asyncio.run(test_learning_telemetry_persistence())
    test_forward_demo_evaluation_provenance()
    test_expert_hypothesis_evidence_classifier()
    asyncio.run(test_expert_knowledge_journal_persistence())
    asyncio.run(test_strategy_evidence_persistence())
    asyncio.run(test_strategy_transition_evidence_persistence())
    asyncio.run(test_model_governance_persistence())
    asyncio.run(test_experiment_engine_persistence())
    asyncio.run(test_chart_activity_notifications())
    asyncio.run(test_capital_reduction_isolation())
    asyncio.run(test_sequential_capital_reduction_planning())
    asyncio.run(test_broker_authoritative_capital_state())
    asyncio.run(test_sizing_rejection_diagnostic_persistence())
    asyncio.run(test_admin_command_error_reply())
    asyncio.run(test_objective_markdown_fallback())
    asyncio.run(test_objective_broker_universe_separation())
    asyncio.run(test_objective_console_safety())
    asyncio.run(test_objective_phase_lifecycle())
    asyncio.run(test_persistent_objective_template_sessions())
    asyncio.run(test_objective_scope_disable_persistence())
    test_session_local_phase_display_number()
    asyncio.run(test_phase_zero_recovery_lifecycle())
    asyncio.run(test_sl_protection_requires_broker_confirmation())
    asyncio.run(test_phase_boundary_closes_unprotected_position())
    asyncio.run(test_phase_boundary_preserves_unprotected_profit())
    asyncio.run(test_legacy_objective_phase_migration())
    test_causal_replay_safety()
    asyncio.run(test_adaptive_management_learning_evidence())
    test_research_governance_rankings()
    asyncio.run(test_same_day_governance_deferral())
    asyncio.run(test_demo_live_partitioning())
    print("PASS: upgrade smoke tests")


if __name__ == "__main__":
    run()
