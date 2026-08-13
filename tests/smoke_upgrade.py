"""Deterministic smoke tests for the causal, basket-risk upgrade.

These tests use fixed local fixtures only. They never connect to MT5, Telegram,
or a market-data service, and they cannot place, modify, or close a trade.
"""

from __future__ import annotations

import asyncio
import os
import sys
import tempfile

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
from analysis.policies import ExperimentalPolicy, HypothesisEngine, PolicyEvaluator, PolicyGenerator
from analysis.account_monitor import summarize_history, exposure_summary
from execution.capital_reduction import CapitalReductionEngine
from analysis.capital_state import AccountCapitalState, CapitalStateService
from analysis.runtime_telemetry import RuntimeTelemetry
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
    below_minimum = manager.calculate_position_sizing(
        account_equity=100.0, free_margin=100.0, entry_price=100.0, stop_loss=98.0,
        symbol_info={**symbol_info, "margin_required_min_volume": 1.0, "normalized_volume": 0.01},
        leverage=100, risk_pct=0.01,
    )
    assert_true(not below_minimum.valid and below_minimum.sizing_code == "MINIMUM_LOT_EXCEEDS_POLICY_RISK", "minimum-lot risk no-fit was not identified")
    assert_true(below_minimum.policy_required_lot < below_minimum.broker_min_lot and below_minimum.minimum_lot_loss > below_minimum.risk_amount, "minimum-lot evidence is incomplete")
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
    asyncio.run(test_broker_authoritative_capital_state())
    asyncio.run(test_sizing_rejection_diagnostic_persistence())
    asyncio.run(test_demo_live_partitioning())
    print("PASS: upgrade smoke tests")


if __name__ == "__main__":
    run()
