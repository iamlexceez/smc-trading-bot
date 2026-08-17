from analysis.market_state_engine import MarketStateEngine
from analysis.regime_engine import RegimeEngine
from analysis.ev_engine import ExpectedValueEngine
from analysis.portfolio_optimizer import PortfolioOptimizer
from analysis.knowledge_engine import KnowledgeSelectionEngine


def test_market_state_engine_builds_valid_state():
    price_data = {"price": 100.0, "spread": 0.1, "quote_age": 1.0}
    analysis_data = {"volatility": 0.5, "trend": "BULLISH", "range_state": "EXPANDING"}
    context_data = {"regime": "TRENDING", "session": "LONDON"}
    
    state = MarketStateEngine.build_state("V75", price_data, analysis_data, context_data)
    assert state.instrument == "V75"
    assert state.regime == "TRENDING"
    assert state.price == 100.0


def test_regime_engine_classifies_trending():
    engine = RegimeEngine()
    features = {"adx": 30.0, "atr_ratio": 1.2, "momentum": 0.5}
    classification = engine.classify("V75", features)
    assert classification.regime == "TRENDING"
    assert classification.confidence > 0.5


def test_ev_engine_calculates_positive_ev():
    engine = ExpectedValueEngine()
    result = engine.calculate_ev(win_rate=0.6, avg_win_r=2.0, avg_loss_r=1.0, costs_r=0.1, sample_size=50)
    # EV = (0.6 * 2.0) - (0.4 * 1.0) - 0.1 = 1.2 - 0.4 - 0.1 = 0.7
    assert round(result.expected_value_r, 1) == 0.7
    assert result.confidence == "GOOD"


def test_portfolio_optimizer_approves_valid_trade():
    optimizer = PortfolioOptimizer()
    portfolio = {"total_risk_pct": 1.0, "instruments": ["V75"]}
    trade = {"instrument": "V50", "risk_pct": 0.5, "ev_r": 0.5, "margin_usd": 10.0}
    impact = optimizer.evaluate_impact(portfolio, trade)
    assert impact.approved is True
    assert "Positive marginal portfolio value" in impact.reason


def test_knowledge_selection_engine_filters_concepts():
    engine = KnowledgeSelectionEngine()
    library = [
        {"knowledge_id": "BOS", "applicability": ["TRENDING"], "conflicts": [], "complements": []},
        {"knowledge_id": "RANGING_STRAT", "applicability": ["RANGING"], "conflicts": [], "complements": []},
    ]
    market_state = {"regime": "TRENDING"}
    selection = engine.select_knowledge(market_state, {}, library)
    assert "BOS" in selection.selected_concepts
    assert "RANGING_STRAT" in selection.rejected_concepts
