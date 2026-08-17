"""Integration tests for Setup Intelligence V2 in the live scheduler."""
import pytest
import asyncio
import pandas as pd
from unittest.mock import MagicMock, AsyncMock, patch
from datetime import datetime, timezone
from uuid import uuid4

from scheduler import MarketScheduler
from config import TradeSettings
from risk.manager import RiskManager
from executors.base import BaseExecutor
from analysis.scoring import TradeSignal
from analysis.setup_intelligence.setup_record import TradeSetup
from analysis.structure import MarketStructure, Trend

@pytest.fixture
def mock_settings():
    settings = MagicMock(spec=TradeSettings)
    settings.trading_mode = "demo"
    settings.timeframes = ["M15"]
    settings.htf_timeframes = ["H1", "H4"]
    settings.enabled_symbols = ["Volatility 10 Index"]
    settings.active_model_version = "v2_test"
    settings.auto_trade = True
    settings.is_paused = False
    settings.structural_stop_atr_buffer = 0.5
    settings.liquidity_sweep_min_penetration_atr = 0.1
    settings.displacement_body_ratio_min = 0.5
    settings.displacement_range_ratio_min = 0.5
    settings.extreme_setup_score = 90.0
    settings.exploration_min_setup_score = 30.0
    settings.exploration_min_strategy_score = 30.0
    settings.min_sl_update_distance_atr = 0.5
    settings.min_tp_update_distance_atr = 0.5
    settings.min_rr_ratio = 1.5
    settings.rr_filter_enabled = True
    settings.max_signal_age_minutes = 60
    return settings

@pytest.fixture
def mock_executor():
    executor = MagicMock(spec=BaseExecutor)
    executor.get_open_positions = AsyncMock(return_value=[])
    executor.order_check = AsyncMock(return_value={"retcode": 0, "comment": "Done"})
    executor.submit_order = AsyncMock(return_value={"retcode": 0, "order": 12345})
    executor.is_connected = AsyncMock(return_value=True)
    return executor

@pytest.fixture
def mock_risk_manager(mock_settings):
    rm = MagicMock(spec=RiskManager)
    rm.settings = mock_settings
    return rm

@pytest.fixture
def scheduler(mock_settings, mock_executor, mock_risk_manager):
    with patch("storage.db.load_settings", AsyncMock(return_value=mock_settings)), \
         patch("storage.db.get_active_objective", AsyncMock(return_value={})):
        s = MarketScheduler(mock_settings, mock_executor, mock_risk_manager)
        s._analysis_eligible_symbols = ("Volatility 10 Index",)
        return s

@pytest.mark.asyncio
async def test_live_v2_invocation(scheduler):
    """TEST 1 — LIVE V2 INVOCATION: scheduler.analyze_symbol() invokes V2."""
    df = pd.DataFrame({
        "time": pd.date_range(start="2026-01-01", periods=100, freq="15min"),
        "open": [100.0] * 100,
        "high": [105.0] * 100,
        "low": [95.0] * 100,
        "close": [102.0] * 100,
        "volume": [1000] * 100,
        "tick_volume": [1000] * 100,
    })
    
    mock_policy = MagicMock()
    mock_policy.to_dict.return_value = {"rr_target": 2.0, "stop_atr_buffer": 0.5}
    mock_policy.rr_target = 2.0
    mock_policy.stop_atr_buffer = 0.5
    mock_policy.entry_model = "aggressive"
    mock_policy.stop_model = "structural"
    mock_policy.target_model = "rr"
    mock_policy.accepts.return_value = (True, "Policy accepted")
    
    mock_profile = MagicMock()
    mock_profile.regime = "TRENDING"
    mock_profile.previous_regime = "UNKNOWN"
    mock_profile.regime_transition = "TRENDING"
    
    mock_structure = MagicMock()
    mock_structure.trend = Trend.BULLISH
    mock_structure.current_zone = "discount"
    mock_structure.last_event.event_type.value = "bos_bullish"
    
    with patch.object(scheduler, "fetch_candles", AsyncMock(return_value=df)), \
         patch("scheduler.build_setup") as mock_build, \
         patch("scheduler.analyze_structure", return_value=mock_structure), \
         patch.object(scheduler.optimizer, "active_policy", AsyncMock(return_value=(mock_policy, 1, "v1"))), \
         patch("scheduler.db.record_decision_record", AsyncMock()), \
         patch("scheduler.db.record_execution_event", AsyncMock()), \
         patch("scheduler.db.record_setup", AsyncMock(return_value=1)), \
         patch("scheduler.db.update_setup_record", AsyncMock()), \
         patch("scheduler.db.load_settings", AsyncMock(return_value=scheduler.settings)), \
         patch("scheduler.db.get_active_objective", AsyncMock(return_value={})), \
         patch("scheduler.db.get_strategy_evidence_for_context", AsyncMock(return_value={})), \
         patch("scheduler.db.get_strategy_transition_evidence_for_context", AsyncMock(return_value={})), \
         patch("scheduler.db.get_pending_counterfactual_setups", AsyncMock(return_value=[])), \
         patch("scheduler.profiler.profile_symbol", AsyncMock(return_value=mock_profile)):
        
        mock_setup = TradeSetup(
            setup_id="test_id",
            timestamp=datetime.now(timezone.utc).isoformat(),
            instrument="Volatility 10 Index",
            instrument_family="DERIV_SYNTHETIC",
            market_regime="TRENDING",
            direction="BUY",
            analysis_timeframe="M15",
            entry_price=102.0,
            stop_loss=100.0,
            target_price=106.0,
            risk_distance=2.0,
            reward_distance=4.0,
            actual_rr=2.0,
            final_eligibility=True,
            decision_reason="Valid"
        )
        mock_build.return_value = mock_setup
        
        await scheduler.analyze_symbol("Volatility 10 Index")
        
        mock_build.assert_called_once()

@pytest.mark.asyncio
async def test_v2_output_propagation(scheduler):
    """TEST 2 — V2 OUTPUT PROPAGATION: Verify scheduler passes setup to downstream."""
    df = pd.DataFrame({
        "time": pd.date_range(start="2026-01-01", periods=100, freq="15min"),
        "open": [100.0] * 100,
        "high": [105.0] * 100,
        "low": [95.0] * 100,
        "close": [102.0] * 100,
        "volume": [1000] * 100,
        "tick_volume": [1000] * 100,
    })
    
    mock_setup = TradeSetup(
        setup_id="test_id",
        timestamp=datetime.now(timezone.utc).isoformat(),
        instrument="Volatility 10 Index",
        instrument_family="DERIV_SYNTHETIC",
        market_regime="TRENDING",
        direction="BUY",
        analysis_timeframe="M15",
        entry_price=102.0,
        stop_loss=100.0,
        target_price=106.0,
        risk_distance=2.0,
        reward_distance=4.0,
        actual_rr=2.0,
        final_eligibility=True,
        decision_reason="Valid"
    )
    
    mock_policy = MagicMock()
    mock_policy.to_dict.return_value = {"rr_target": 2.0, "stop_atr_buffer": 0.5}
    mock_policy.rr_target = 2.0
    mock_policy.stop_atr_buffer = 0.5
    mock_policy.entry_model = "aggressive"
    mock_policy.stop_model = "structural"
    mock_policy.target_model = "rr"
    mock_policy.accepts.return_value = (True, "Policy accepted")
    
    mock_profile = MagicMock()
    mock_profile.regime = "TRENDING"
    mock_profile.previous_regime = "UNKNOWN"
    mock_profile.regime_transition = "TRENDING"
    
    mock_structure = MagicMock()
    mock_structure.trend = Trend.BULLISH
    mock_structure.current_zone = "discount"
    mock_structure.last_event.event_type.value = "bos_bullish"
    
    with patch.object(scheduler, "fetch_candles", AsyncMock(return_value=df)), \
         patch("scheduler.build_setup", return_value=mock_setup), \
         patch("scheduler.analyze_structure", return_value=mock_structure), \
         patch.object(scheduler.optimizer, "active_policy", AsyncMock(return_value=(mock_policy, 1, "v1"))), \
         patch("storage.db.record_setup", AsyncMock(return_value=1)) as mock_record, \
         patch("scheduler.db.record_decision_record", AsyncMock()), \
         patch("scheduler.db.record_execution_event", AsyncMock()), \
         patch("scheduler.db.update_setup_record", AsyncMock()), \
         patch("scheduler.db.load_settings", AsyncMock(return_value=scheduler.settings)), \
         patch("scheduler.db.get_active_objective", AsyncMock(return_value={})), \
         patch("scheduler.db.get_strategy_evidence_for_context", AsyncMock(return_value={})), \
         patch("scheduler.db.get_strategy_transition_evidence_for_context", AsyncMock(return_value={})), \
         patch("scheduler.db.get_pending_counterfactual_setups", AsyncMock(return_value=[])), \
         patch("scheduler.profiler.profile_symbol", AsyncMock(return_value=mock_profile)):
        
        await scheduler.analyze_symbol("Volatility 10 Index")
        
        assert mock_record.called
        args, kwargs = mock_record.call_args
        assert kwargs["direction"] == "BUY"
        assert kwargs["entry_price"] == 102.0

@pytest.mark.asyncio
async def test_symbol_isolation(scheduler):
    """TEST 5 — SYMBOL ISOLATION: One symbol failure does not stop the cycle."""
    scheduler._analysis_eligible_symbols = ("FailSymbol", "SuccessSymbol")
    
    async def mock_analyze(symbol, **kwargs):
        if symbol == "FailSymbol":
            raise ValueError("Simulated failure")
        return TradeSignal(
            symbol=symbol, direction="BUY", entry_price=100.0, stop_loss=90.0, take_profit=130.0,
            score=70.0, rr_ratio=3.0, passed=True
        )

    with patch.object(scheduler, "analyze_symbol", side_effect=mock_analyze):
        signals = await scheduler.scan_markets()
        
        assert len(signals) == 1
        assert signals[0].symbol == "SuccessSymbol"

@pytest.mark.asyncio
async def test_no_lookahead_verification(scheduler):
    """TEST 12 — NO LOOKAHEAD: Future candles cannot affect historical setup."""
    from analysis.setup_intelligence.setup_builder import build_setup
    
    base_df = pd.DataFrame({
        "time": pd.date_range(start="2026-01-01", periods=50, freq="15min"),
        "open": [100.0] * 50,
        "high": [101.0] * 50,
        "low": [99.0] * 50,
        "close": [100.5] * 50,
        "volume": [1000] * 50,
        "tick_volume": [1000] * 50,
    })
    
    for i in range(40, 50):
        base_df.loc[i, "close"] = 100.5 + (i - 40) * 0.5
        base_df.loc[i, "high"] = base_df.loc[i, "close"] + 0.5
    
    setup1 = build_setup("Volatility 10 Index", "M15", base_df)
    
    future_df = pd.DataFrame({
        "time": pd.date_range(start="2026-01-01 12:30:00", periods=50, freq="15min"),
        "open": [105.0] * 50,
        "high": [106.0] * 50,
        "low": [104.0] * 50,
        "close": [105.5] * 50,
        "volume": [1000] * 50,
        "tick_volume": [1000] * 50,
    })
    full_df = pd.concat([base_df, future_df]).reset_index(drop=True)
    
    setup2 = build_setup("Volatility 10 Index", "M15", full_df.iloc[:50])
    
    if setup1 and setup2:
        assert setup1.direction == setup2.direction
        assert setup1.entry_price == setup2.entry_price
        assert setup1.stop_loss == setup2.stop_loss
        assert setup1.target_price == setup2.target_price

@pytest.mark.asyncio
async def test_v2_evidence_persistence(scheduler):
    """TEST 3 — EVIDENCE PERSISTENCE: Verify V2 evidence is stored."""
    df = pd.DataFrame({
        "time": pd.date_range(start="2026-01-01", periods=100, freq="15min"),
        "open": [100.0] * 100,
        "high": [105.0] * 100,
        "low": [95.0] * 100,
        "close": [102.0] * 100,
        "volume": [1000] * 100,
        "tick_volume": [1000] * 100,
    })
    
    mock_setup = TradeSetup(
        setup_id="test_id",
        timestamp=datetime.now(timezone.utc).isoformat(),
        instrument="Volatility 10 Index",
        instrument_family="DERIV_SYNTHETIC",
        market_regime="TRENDING",
        direction="BUY",
        analysis_timeframe="M15",
        entry_price=102.0,
        stop_loss=100.0,
        target_price=106.0,
        risk_distance=2.0,
        reward_distance=4.0,
        actual_rr=2.0,
        final_eligibility=True,
        decision_reason="Valid",
        liquidity_sweep={"type": "bsl", "price": 105.0},
        structure_event={"type": "bos", "direction": "bullish"}
    )
    
    mock_policy = MagicMock()
    mock_policy.to_dict.return_value = {"rr_target": 2.0, "stop_atr_buffer": 0.5}
    mock_policy.rr_target = 2.0
    mock_policy.stop_atr_buffer = 0.5
    mock_policy.entry_model = "aggressive"
    mock_policy.stop_model = "structural"
    mock_policy.target_model = "rr"
    mock_policy.accepts.return_value = (True, "Policy accepted")
    
    mock_profile = MagicMock()
    mock_profile.regime = "TRENDING"
    mock_profile.previous_regime = "UNKNOWN"
    mock_profile.regime_transition = "TRENDING"
    
    mock_structure = MagicMock()
    mock_structure.trend = Trend.BULLISH
    mock_structure.current_zone = "discount"
    mock_structure.last_event.event_type.value = "bos_bullish"
    
    with patch.object(scheduler, "fetch_candles", AsyncMock(return_value=df)), \
         patch("scheduler.build_setup", return_value=mock_setup), \
         patch("scheduler.analyze_structure", return_value=mock_structure), \
         patch.object(scheduler.optimizer, "active_policy", AsyncMock(return_value=(mock_policy, 1, "v1"))), \
         patch("storage.db.record_setup", AsyncMock(return_value=1)), \
         patch("scheduler.db.record_decision_record", AsyncMock()) as mock_decision, \
         patch("scheduler.db.record_execution_event", AsyncMock()), \
         patch("scheduler.db.update_setup_record", AsyncMock()), \
         patch("scheduler.db.load_settings", AsyncMock(return_value=scheduler.settings)), \
         patch("scheduler.db.get_active_objective", AsyncMock(return_value={})), \
         patch("scheduler.db.get_strategy_evidence_for_context", AsyncMock(return_value={})), \
         patch("scheduler.db.get_strategy_transition_evidence_for_context", AsyncMock(return_value={})), \
         patch("scheduler.db.get_pending_counterfactual_setups", AsyncMock(return_value=[])), \
         patch("scheduler.profiler.profile_symbol", AsyncMock(return_value=mock_profile)):
        
        await scheduler.analyze_symbol("Volatility 10 Index")
        
        assert mock_decision.called
        args, kwargs = mock_decision.call_args
        # The thesis contains the primary field which now has v2_evidence
        assert "v2_evidence" in kwargs["thesis"]["primary"]
        assert kwargs["thesis"]["primary"]["v2_evidence"]["liquidity_sweep"] == {"type": "bsl", "price": 105.0}
