import pytest
import asyncio
from unittest.mock import MagicMock, patch
from analysis.decision_gates import evaluate_trading_gate
# OpportunityRanker removed, using rank_opportunities if needed
from knowledge.instruments import classify_instrument
from knowledge.specialization import evidence_tier
from config import TradeSettings

@pytest.fixture
def mock_settings():
    settings = TradeSettings.defaults()
    settings.trading_mode = "demo"
    settings.exploration_enabled = True
    settings.exploration_min_setup_score = 40.0
    settings.normal_demo_min_setup_score = 50.0
    settings.auto_trade = True
    settings.is_paused = False
    return settings

@pytest.mark.asyncio
async def test_score_50_demo_eligible(mock_settings):
    """TEST 1: Score 50 setup can become DEMO eligible."""
    decision = evaluate_trading_gate(
        setup_valid=True,
        broker_symbol_valid=True,
        valid_market_data=True,
        objective_permits_exposure=True,
        evidence={"sample_size": 0},
        champion_governed=False,
        setup_quality=50.0,
        exploratory_threshold=40.0,
        demo_mode=True,
        exploration_authorized=True,
        strategy_quality=60.0,
        strategy_threshold=40.0,
        actual_rr=2.0,
        minimum_rr=1.5,
        rr_filter_enabled=True
    )
    assert decision.trading_decision == "CONTROLLED_FORWARD_DEMO"
    assert decision.execution_class == "NORMAL"

@pytest.mark.asyncio
async def test_score_45_experimental_eligible(mock_settings):
    """TEST 2: Score 45 setup can become EXPERIMENTAL DEMO eligible."""
    decision = evaluate_trading_gate(
        setup_valid=True,
        broker_symbol_valid=True,
        valid_market_data=True,
        objective_permits_exposure=True,
        evidence={"sample_size": 0},
        champion_governed=False,
        setup_quality=45.0,
        exploratory_threshold=40.0,
        demo_mode=True,
        exploration_authorized=True,
        strategy_quality=60.0,
        strategy_threshold=40.0,
        actual_rr=2.0,
        minimum_rr=1.5,
        rr_filter_enabled=True
    )
    assert decision.trading_decision == "CONTROLLED_FORWARD_DEMO"
    assert decision.execution_class == "EXPERIMENTAL"

@pytest.mark.asyncio
async def test_score_39_ineligible(mock_settings):
    """TEST 3: Score 39 setup remains ineligible."""
    decision = evaluate_trading_gate(
        setup_valid=True,
        broker_symbol_valid=True,
        valid_market_data=True,
        objective_permits_exposure=True,
        evidence={"sample_size": 0},
        champion_governed=False,
        setup_quality=39.0,
        exploratory_threshold=40.0,
        demo_mode=True,
        exploration_authorized=True,
        strategy_quality=60.0,
        strategy_threshold=40.0,
        actual_rr=2.0,
        minimum_rr=1.5,
        rr_filter_enabled=True
    )
    assert decision.trading_decision in ["NO_TRADE", "EXECUTION_BLOCKED"]
    assert "SETUP_TOO_WEAK" in decision.reason_codes

@pytest.mark.asyncio
async def test_insufficient_evidence_does_not_block(mock_settings):
    """TEST 4 & 5: INSUFFICIENT evidence / UNVALIDATED confidence does not block DEMO."""
    decision = evaluate_trading_gate(
        setup_valid=True,
        broker_symbol_valid=True,
        valid_market_data=True,
        objective_permits_exposure=True,
        evidence={"sample_size": 0},
        champion_governed=False,
        setup_quality=55.0,
        exploratory_threshold=40.0,
        demo_mode=True,
        exploration_authorized=True,
        strategy_quality=60.0,
        strategy_threshold=40.0,
        actual_rr=2.0,
        minimum_rr=1.5,
        rr_filter_enabled=True
    )
    assert decision.trading_decision == "CONTROLLED_FORWARD_DEMO"
    assert "EVIDENCE_COLLECTION_REQUIRED" in decision.reason_codes

def test_instrument_lifecycle():
    """TEST 11, 12, 13: Instrument lifecycle promotion/demotion."""
    # Exploratory
    c1 = classify_instrument("V75", broker_eligible=True, evidence={"sample_size": 5}, minimum_sample_size=50, max_manageable_drawdown_r=2.0, minimum_execution_reliability=0.95)
    assert c1.role == "EXPLORATORY"
    
    # Candidate
    c2 = classify_instrument("V75", broker_eligible=True, evidence={"sample_size": 20, "expectancy_r": 0.5}, minimum_sample_size=50, max_manageable_drawdown_r=2.0, minimum_execution_reliability=0.95)
    assert c2.role == "CANDIDATE"
    
    # Core
    c3 = classify_instrument("V75", broker_eligible=True, evidence={"sample_size": 60, "expectancy_r": 0.5, "max_drawdown_r": 1.0, "execution_reliability": 0.98}, minimum_sample_size=50, max_manageable_drawdown_r=2.0, minimum_execution_reliability=0.95)
    assert c3.role == "CORE"
    
    # Under Review (Negative Expectancy)
    c4 = classify_instrument("V75", broker_eligible=True, evidence={"sample_size": 60, "expectancy_r": -0.2}, minimum_sample_size=50, max_manageable_drawdown_r=2.0, minimum_execution_reliability=0.95)
    assert c4.role == "UNDER_REVIEW"
    
    # Under Review (High Drawdown)
    c5 = classify_instrument("V75", broker_eligible=True, evidence={"sample_size": 60, "expectancy_r": 0.5, "max_drawdown_r": 5.0}, minimum_sample_size=50, max_manageable_drawdown_r=2.0, minimum_execution_reliability=0.95)
    assert c5.role == "UNDER_REVIEW"

def test_sample_tiers():
    assert evidence_tier(5) == "UNKNOWN"
    assert evidence_tier(15) == "EARLY"
    assert evidence_tier(30) == "DEVELOPING"
    assert evidence_tier(60) == "MEANINGFUL"
    assert evidence_tier(150) == "STRONG"
