from analysis.decision_gates import evaluate_trading_gate
from analysis.setup_intelligence.setup_record import TradeSetup
from analysis.setup_intelligence.setup_validator import validate_setup


def test_v2_geometry_validity_does_not_discard_low_rr_demo_candidate():
    setup = TradeSetup(
        setup_id="test-low-rr", timestamp="2026-08-22T00:00:00+00:00",
        instrument="Step Index 500", instrument_family="DERIV_SYNTHETIC",
        market_regime="TRENDING", direction="BUY", analysis_timeframe="M15",
        htf_bias="bullish", liquidity_sweep={}, structure_event={},
        poi_type="order_block", poi_price=100.0, poi_high=101.0, poi_low=99.0,
        trigger_type="displacement_close", trigger_price=100.0, entry_price=100.0,
        invalidation_price=90.0, stop_loss=90.0, target_type="structured_rr_target",
        target_price=110.0, risk_distance=10.0, reward_distance=10.0, actual_rr=1.0,
        setup_model="causal_sequence_v2", created_at="2026-08-22T00:00:00+00:00",
        updated_at="2026-08-22T00:00:00+00:00",
    )
    valid, reason = validate_setup(setup, min_rr=1.5)
    assert valid is True
    assert "deferred to downstream execution governance" in reason


def test_demo_low_rr_is_exploration_not_policy_failure():
    decision = evaluate_trading_gate(
        setup_valid=True, broker_symbol_valid=True, valid_market_data=True,
        objective_permits_exposure=True,
        evidence={"sample_size": 0, "confidence": "UNKNOWN"},
        champion_governed=False, forward_demo_experiment_allowed=True,
        portfolio_approved=True, required_htf_context_available=True,
        setup_quality=50.0, exploratory_threshold=50.0, demo_mode=True,
        experiment_id=1, exploration_authorized=True, strategy_quality=50.0,
        strategy_threshold=50.0, actual_rr=1.0, minimum_rr=1.5,
        rr_filter_enabled=True, risk_valid=True, capital_efficiency_approved=True,
    )
    assert decision.trading_decision == "CONTROLLED_FORWARD_DEMO"
    assert decision.final_state == "EXPLORATORY_DEMO"
    assert decision.execution_class == "EXPLORATION"


def test_live_low_rr_still_remains_blocked():
    decision = evaluate_trading_gate(
        setup_valid=True, broker_symbol_valid=True, valid_market_data=True,
        objective_permits_exposure=True,
        evidence={"sample_size": 0, "confidence": "UNKNOWN"},
        champion_governed=False, portfolio_approved=True,
        required_htf_context_available=True, setup_quality=90.0,
        demo_mode=False, exploration_authorized=False, strategy_quality=90.0,
        actual_rr=1.0, minimum_rr=1.5, rr_filter_enabled=True,
        risk_valid=True, capital_efficiency_approved=True,
    )
    assert decision.trading_decision in {"NO_TRADE", "EXECUTION_BLOCKED", "TRADE_REJECTED"}
    assert decision.execution_class != "EXPLORATION"
