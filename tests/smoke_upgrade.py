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
import scheduler  # noqa: F401 — validates live-pipeline imports without starting it.
from bot.handlers import BotHandlers  # noqa: F401 — validates Telegram control imports.


def assert_true(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)


def test_config_round_trip() -> None:
    settings = TradeSettings.defaults()
    encoded = settings.to_dict()
    restored = TradeSettings.from_dict(encoded)
    assert_true(restored.entry_mode == settings.entry_mode, "entry mode did not round-trip")
    assert_true(restored.layer_allocation == settings.layer_allocation, "layer allocation did not round-trip")
    assert_true(restored.max_setup_risk_pct <= 1.0, "setup risk cap must be at most 1%")
    assert_true(restored.chart_activity_level == "detailed", "detailed chart activity must be the default")

    legacy = TradeSettings.from_dict({"risk_per_trade": 10.0, "max_daily_loss_pct": 20.0, "max_open_positions": 5, "score_threshold": 60.0})
    assert_true(legacy.risk_per_trade == 1.0, "legacy risk was not capped")
    assert_true(legacy.max_daily_loss_pct == 3.0, "legacy daily loss was not safely migrated")
    assert_true(legacy.max_open_positions == 2, "legacy position cap was not safely migrated")
    assert_true(legacy.min_setup_score == 0.0, "learning baseline did not remove the quality-score entry gate")


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
    assert_true(sizing.expected_loss <= sizing.risk_amount + 1e-6, "sizing exceeded budget")
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
                {"name": "DERIV-GOLD", "description": "Gold", "path": "Metals\\Gold", "trade_mode": 1, "available": True},
                {"name": "UnsupportedMarket", "description": "Unsupported broker market", "path": "Other\\Market", "trade_mode": 1, "available": True},
                {"name": "Crash 500 Index", "description": "Crash 500 Index", "path": "Synthetic Indices\\Crash", "trade_mode": 0, "available": False},
            ]

    universe = DerivMarketUniverse()
    await universe.refresh(FakeBroker())
    assert_true(universe.available_symbols == ["DERIV-GOLD", "Volatility 75 Index"], "eligible Deriv markets were not classified correctly")
    assert_true(universe.status_for("Crash 500 Index") == "unavailable", "unavailable broker symbol became active")
    assert_true("UnsupportedMarket" in universe.unsupported_symbols, "unsupported broker symbol was not excluded from the Deriv universe")


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
            parameters={"min_setup_score": 80.0, "preferred_risk_pct": 0.50},
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
    test_config_round_trip()
    test_risk_sizing_and_layers()
    test_no_widening_management()
    test_causal_confirmation_invariants()
    asyncio.run(test_broker_only_data_provider())
    asyncio.run(test_deriv_market_universe())
    asyncio.run(test_basket_persistence())
    asyncio.run(test_learning_telemetry_persistence())
    asyncio.run(test_model_governance_persistence())
    asyncio.run(test_chart_activity_notifications())
    asyncio.run(test_demo_live_partitioning())
    print("PASS: upgrade smoke tests")


if __name__ == "__main__":
    run()
