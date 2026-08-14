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
from analysis.liquidity import build_liquidity_pools
from execution.manager import ManagementState, TradeManager
from executors.base import Position
from risk.manager import RiskManager
from storage import db
from data.universe import DerivMarketUniverse
from data.provider import DataProvider
from analysis.optimizer import SelfOptimizer
from analysis.research_governance import ResearchGovernance
from analysis.adaptive_management import observation_from_broker_trade, observations_from_backtest, summarize_management
from analysis.objectives import ObjectiveInterpreter, ObjectiveValidator, TradingObjective, phase_for_equity
from backtest.engine import BacktestEngine, BacktestResult, BacktestTrade
from analysis.policies import ExperimentalPolicy, HypothesisEngine, PolicyEvaluator, PolicyGenerator
from analysis.account_monitor import summarize_history, exposure_summary
from execution.capital_reduction import CapitalReductionEngine
from execution import capital_reduction as capital_reduction_module
from analysis.capital_state import AccountCapitalState, CapitalStateService
from analysis.runtime_telemetry import RuntimeTelemetry
from strategy.setup_validator import calculate_rr, rr_filter_passes
import scheduler  # noqa: F401 — validates live-pipeline imports without starting it.
from bot.handlers import BotHandlers  # noqa: F401 — validates Telegram control imports.


def assert_true(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)


def test_runtime_telemetry() -> None:
    telemetry = RuntimeTelemetry()
    telemetry.component_started("market_scanner")
    telemetry.increment("scan_cycles_started")
    telemetry.increment("symbols_attempted", 3)
    telemetry.increment("candle_requests", 6)
    telemetry.record_timeframe("M15", 3)
    telemetry.record_rejection("No directional structure")
    telemetry.component_succeeded("market_scanner")
    first = telemetry.heartbeat_snapshot_and_reset()
    assert_true(first["window"]["counters"]["scan_cycles_started"] == 1, "heartbeat window lost a real scan start")
    assert_true(first["window"]["counters"]["symbols_attempted"] == 3 and first["window"]["timeframes"]["M15"] == 3, "runtime symbol/timeframe evidence is incorrect")
    after = telemetry.snapshot()
    assert_true(after["window"]["counters"]["scan_cycles_started"] == 0, "heartbeat did not reset its activity window")
    assert_true(after["lifetime"]["counters"]["scan_cycles_started"] == 1, "lifetime telemetry was incorrectly reset")
    telemetry.component_started("analysis_engine")
    telemetry.component_failed("analysis_engine", RuntimeError("fixture failure"))
    assert_true(after["components"]["market_scanner"]["last_success"], "component success state was not retained")
    assert_true(telemetry.snapshot()["components"]["analysis_engine"]["state"] == "FAILED", "component failure was not exposed")


def test_full_precision_rr_validation() -> None:
    risk, reward, rr = calculate_rr("SELL", 4350.274, 4402.92955, 4343.897)
    assert_true(abs(risk - 52.65555) < 1e-6 and abs(reward - 6.377) < 1e-6, "SELL RR distances were calculated incorrectly")
    assert_true(abs(rr - 0.121107841433619) < 1e-9 and rr < 3.0, "exact low-RR SELL fixture was not rejected by full precision")
    assert_true(not rr_filter_passes(rr, 3.0), "configured 3R filter did not reject the exact low-RR SELL fixture")
    assert_true(rr_filter_passes(rr, 0.0), "zero configured RR did not disable RR rejection")
    assert_true(rr_filter_passes(rr, -1.0), "negative RR configuration was not treated as disabled")
    buy_risk, buy_reward, buy_rr = calculate_rr("BUY", 100.0, 98.0, 106.0)
    assert_true(buy_risk == 2.0 and buy_reward == 6.0 and buy_rr == 3.0, "BUY RR formula is incorrect")
    assert_true(rr_filter_passes(buy_rr, 3.0) and not rr_filter_passes(buy_rr, 3.1), "positive configured RR filtering is incorrect")


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
    assert_true(overshoot_plan is None and "target/tolerance" in overshoot_reason, "minimum-loss action that crosses target tolerance was not blocked")

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

        async def get_account_info(self):
            return next(self.accounts)

        async def execute_trade(self, **kwargs):
            self.submissions += 1
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
        strategy_trade = await db.record_trade("Volatility 75 Index", "BUY", 100.0, 99.0, 102.0, 0.1, 0.0, 2.0, "mt5", "{}", ticket=777, db_path=path)
        await db.close_trade(strategy_trade, -3.0, path)
        await db.record_capital_reduction_action(session_id=session_id, action="order_filled", status="open", ticket=777, symbol="Volatility 75 Index", direction="BUY", volume=0.1, db_path=path)
        outcomes = await db.get_strategy_trade_outcomes_excluding_capital_reduction(db_path=path)
        assert_true(not outcomes, "capital-reduction ticket contaminated strategy-learning outcomes")
        await db.update_capital_reduction_session(session_id, status="completed", current_equity=505.0, current_balance=505.0, capital_test_active=True, db_path=path)
        completed = await db.get_capital_reduction_session(session_id, path)
        assert_true(completed and completed["capital_test_active"] and completed["status"] == "completed", "capital-test transition was not persisted")


async def test_broker_authoritative_capital_state() -> None:
    class FakeBroker:
        def __init__(self) -> None:
            self.available = True
            self.balance = 100.0
            self.equity = 100.0
            self.free_margin = 100.0
            self.margin_level = 1_000.0

        async def get_live_account_snapshot(self, history_days=0):
            if not self.available:
                return {"current": False, "error": "MT5 unavailable"}
            return {
                "current": True, "retrieved_at": "2026-08-13T10:00:00Z",
                "account": {
                    "login": 123456, "broker_account_mode": "demo", "balance": self.balance,
                    "equity": self.equity, "free_margin": self.free_margin,
                    "margin_level": self.margin_level, "margin_so_call": 100.0,
                    "margin_so_so": 50.0, "leverage": 100, "currency": "USD",
                },
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
        assert_true(critical["state"] == AccountCapitalState.CRITICAL_CAPITAL and critical["state"] in AccountCapitalState.BLOCKING, "broker margin-call condition did not halt execution as capital-critical")

        broker.margin_level = 1_000.0
        broker.free_margin = 5.0
        exhausted = await service.evaluate()
        exhausted_symbol = ((exhausted.get("broker_metadata") or {}).get("symbols") or [{}])[0]
        assert_true(exhausted_symbol.get("specification_valid") and not exhausted_symbol.get("usable"), "valid symbol specification was lost when only current free margin became insufficient")
        assert_true(exhausted["state"] == AccountCapitalState.CAPITAL_EXHAUSTED, "insufficient broker free margin was not classified as functional exhaustion")
        persisted = await db.get_account_state("demo", path)
        assert_true(persisted and persisted["state"] == AccountCapitalState.CAPITAL_EXHAUSTED, "exhausted state was not persisted authoritatively")

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

    below_target = interpreter.parse("Start with $100 and aim for $50", account_mode="demo")
    below_validation = ObjectiveValidator.validate(below_target, account_snapshot=account, account_state="ACCOUNT_VERIFIED", broker_usable_symbols=usable)
    assert_true(not below_validation.valid and below_validation.errors, "target below starting capital remained confirmable")

    unsupported = interpreter.parse("Start with $50 and aim for $100 with XAUEUR", account_mode="demo")
    unsupported_validation = ObjectiveValidator.validate(unsupported, account_snapshot=account, account_state="ACCOUNT_VERIFIED", broker_usable_symbols=usable)
    assert_true(not unsupported_validation.valid and any("XAUEUR" in error for error in unsupported_validation.errors), "unsupported broker symbol was not blocked")
    assert_true(not hasattr(interpreter, "executor") and not hasattr(interpreter, "mt5"), "objective parser was given an execution boundary")

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
        {"time": pd.Timestamp("2026-01-01T00:00:00Z") + pd.Timedelta(minutes=index), "open": 100.0, "high": 100.1, "low": 99.9, "close": 100.0, "volume": 1.0}
        for index in range(29)
    ] + [{"time": pd.Timestamp("2026-01-01T00:29:00Z"), "open": 100.0, "high": 101.1, "low": 99.2, "close": 101.0, "volume": 1.0}])
    management.replay_management_bar(history, len(history) - 1, 0.01)
    assert_true(management.open_trade.sl_modifications == 1 and management.open_trade.breakeven_activated, "replay management action was not recorded from the existing TradeManager")

    bars = pd.DataFrame([
        {"time": pd.Timestamp("2026-01-02T00:00:00Z") + pd.Timedelta(minutes=index), "open": 100.0 + index * 0.01, "high": 100.1 + index * 0.01, "low": 99.9 + index * 0.01, "close": 100.0 + index * 0.01, "volume": 1.0}
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


def run() -> None:
    test_runtime_telemetry()
    test_full_precision_rr_validation()
    asyncio.run(test_single_flight_scan_guard())
    test_scanner_eligibility_handoff()
    test_config_round_trip()
    test_account_monitor_aggregates()
    test_risk_sizing_and_layers()
    test_no_widening_management()
    test_causal_confirmation_invariants()
    asyncio.run(test_broker_only_data_provider())
    asyncio.run(test_deriv_market_universe())
    asyncio.run(test_basket_persistence())
    asyncio.run(test_learning_telemetry_persistence())
    asyncio.run(test_model_governance_persistence())
    asyncio.run(test_experiment_engine_persistence())
    asyncio.run(test_chart_activity_notifications())
    asyncio.run(test_capital_reduction_isolation())
    asyncio.run(test_sequential_capital_reduction_planning())
    asyncio.run(test_broker_authoritative_capital_state())
    asyncio.run(test_sizing_rejection_diagnostic_persistence())
    asyncio.run(test_objective_console_safety())
    test_causal_replay_safety()
    asyncio.run(test_adaptive_management_learning_evidence())
    test_research_governance_rankings()
    asyncio.run(test_same_day_governance_deferral())
    asyncio.run(test_demo_live_partitioning())
    print("PASS: upgrade smoke tests")


if __name__ == "__main__":
    run()
