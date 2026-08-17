from knowledge.context import build_context, normalize_regime
from knowledge.registry import all_items
from knowledge.router import route_context


def test_regime_aliases_normalize_to_controlled_vocabulary():
    assert normalize_regime("trending") == "TRENDING_BULLISH"
    assert normalize_regime("compression") == "CONTRACTION"
    assert normalize_regime("not-a-regime") == "UNKNOWN"


def test_registry_contains_machine_readable_hypotheses():
    items = all_items()
    assert items
    assert all(item.knowledge_id and item.category and item.status == "HYPOTHESIS" for item in items)
    assert any("liquidity_sweep" in item.required_features for item in items)


def test_router_selects_contextual_candidates_and_keeps_unvalidated_evidence_explicit():
    context = build_context(
        "Boom 500 Index", "M15", regime="TRENDING", htf_bias="bullish",
        top_down_alignment="aligned",
        observed_features=("liquidity_sweep", "displacement", "htf_alignment", "structure_event"),
    )
    route = route_context(context, evidence_by_strategy={"liquidity_sweep_continuation": {"sample_size": 0}})
    assert route.regime == "TRENDING_BULLISH"
    assert route.candidate_strategies
    assert "liquidity_sweep_continuation" in route.candidate_strategies
    selected = next(item for item in route.routes if item.identifier == "liquidity_sweep_continuation")
    assert selected.evidence_state == "UNVALIDATED"
    assert route.knowledge_ids


def test_router_rejects_missing_features_without_authorizing_a_trade():
    context = build_context("Boom 500 Index", "M15", regime="BREAKOUT", observed_features=())
    route = route_context(context)
    assert "displacement_continuation" in {row["strategy"] for row in route.rejected_strategies}
    assert route.candidate_strategies == ()


def test_combination_evidence_distinguishes_missing_and_incremental_value():
    from knowledge.combinations import evaluate_combination

    missing = evaluate_combination(
        "liquidity_sweep+displacement",
        single_a={"expectancy_r": 0.2, "sample_size": 30},
        single_b=None,
        combined={"expectancy_r": 0.5, "sample_size": 30},
        minimum_sample_size=20,
    )
    assert missing.state == "INSUFFICIENT_EVIDENCE"

    redundant = evaluate_combination(
        "a+b",
        single_a={"expectancy_r": 0.4},
        single_b={"expectancy_r": 0.2},
        combined={"expectancy_r": 0.41, "sample_size": 30},
        minimum_sample_size=20,
        minimum_incremental_expectancy_r=0.1,
    )
    assert redundant.state == "REDUNDANT"

    conflicting = evaluate_combination(
        "a+b",
        single_a={"expectancy_r": 0.4},
        single_b={"expectancy_r": 0.2},
        combined={"expectancy_r": 0.1, "sample_size": 30},
        minimum_sample_size=20,
    )
    assert conflicting.state == "CONFLICTING"

    promotable = evaluate_combination(
        "a+b",
        single_a={"expectancy_r": 0.2},
        single_b={"expectancy_r": 0.1},
        combined={"expectancy_r": 0.5, "sample_size": 30},
        minimum_sample_size=20,
        minimum_incremental_expectancy_r=0.1,
    )
    assert promotable.state == "PROMOTABLE_CANDIDATE"


def test_economic_regimes_and_geometric_growth_are_explicit():
    from knowledge.economics import classify_economic_regime, geometric_growth

    assert classify_economic_regime(20).name == "SURVIVAL"
    assert classify_economic_regime(40).name == "CONTROLLED_GROWTH"
    assert classify_economic_regime(75).name == "GROWTH"
    assert classify_economic_regime(150).name == "EXPANSION"
    assert geometric_growth([0.1, -0.05]) == 1.045


def test_instrument_roles_distinguish_unknown_negative_and_core_evidence():
    from knowledge.instruments import classify_instrument

    assert classify_instrument(
        "Boom 500 Index", broker_eligible=True, evidence={}, minimum_sample_size=30,
        max_manageable_drawdown_r=2.0, minimum_execution_reliability=0.95,
    ).role == "RESEARCH"
    assert classify_instrument(
        "Boom 500 Index", broker_eligible=True,
        evidence={"sample_size": 30, "expectancy_r": -0.1}, minimum_sample_size=30,
        max_manageable_drawdown_r=2.0, minimum_execution_reliability=0.95,
    ).role == "QUARANTINED"
    assert classify_instrument(
        "Boom 500 Index", broker_eligible=True,
        evidence={"sample_size": 30, "expectancy_r": 0.3, "max_drawdown_r": 1.0, "execution_reliability": 0.99},
        minimum_sample_size=30, max_manageable_drawdown_r=2.0, minimum_execution_reliability=0.95,
    ).role == "CORE"


def test_model_drift_distinguishes_sparse_data_from_deterioration():
    from analysis.drift import detect_model_drift

    assert detect_model_drift(
        {"expectancy_r": 0.4, "sample_size": 30}, {"expectancy_r": -0.5, "sample_size": 2},
        minimum_sample_size=10, max_expectancy_decline_r=0.2,
    ).state == "INSUFFICIENT_EVIDENCE"
    assert detect_model_drift(
        {"expectancy_r": 0.4, "sample_size": 30}, {"expectancy_r": 0.1, "sample_size": 10},
        minimum_sample_size=10, max_expectancy_decline_r=0.2,
    ).state == "DRIFT"
    assert detect_model_drift(
        {"expectancy_r": 0.4, "sample_size": 30}, {"expectancy_r": 0.3, "sample_size": 10},
        minimum_sample_size=10, max_expectancy_decline_r=0.2,
    ).state == "STABLE"


def test_instrument_specialization_score_is_not_opportunity_score():
    from knowledge.specialization import evidence_tier, expectancy_r, score_specialization

    assert round(expectancy_r(0.5, 2.0, 1.0), 6) == 0.5
    assert evidence_tier(0) == "UNVALIDATED"
    assert evidence_tier(19) == "VERY_WEAK"
    assert evidence_tier(20) == "PRELIMINARY"
    assert evidence_tier(100) == "ESTABLISHED"
    assert evidence_tier(200) == "STRONG"

    components = {
        "statistical_performance": 90,
        "out_of_sample_performance": 85,
        "forward_demo_performance": 80,
        "stability": 75,
        "regime_coverage": 70,
        "execution_quality": 95,
        "account_size_suitability": 90,
        "portfolio_contribution": 80,
    }
    mature = score_specialization(
        "Boom 500 Index", components=components, sample_size=200,
        out_of_sample_sample=100, forward_sample=100, recency_factor=1.0,
        data_quality_factor=1.0,
    )
    weak = score_specialization(
        "Boom 500 Index", components=components, sample_size=2,
        out_of_sample_sample=0, forward_sample=0, recency_factor=1.0,
        data_quality_factor=1.0,
    )
    assert mature.raw_score > 80
    assert mature.adjusted_score > weak.adjusted_score
    assert weak.evidence_tier == "VERY_WEAK"
    assert "opportunity" not in " ".join(mature.reasons).lower()


def test_specialization_governance_requires_complete_core_evidence_and_does_not_pad_slots():
    from analysis.research_governance import ResearchGovernance
    from config import TradeSettings

    settings = TradeSettings.defaults()
    settings.max_core_instruments = 1
    settings.core_min_sample_size = 50
    settings.core_adjusted_score_threshold = 75.0
    governance = ResearchGovernance(settings)
    outcomes = [
        {"symbol": "Boom 500 Index", "pnl_r": 1.0, "regime": "TRENDING"}
        for _ in range(200)
    ] + [
        {"symbol": "Boom 100 Index", "pnl_r": 1.0, "regime": "TRENDING"}
        for _ in range(200)
    ]
    incomplete = governance.rank_instrument_specialization(
        ["Boom 500 Index", "Boom 100 Index"], outcomes,
        instrument_metadata={"Boom 500 Index": {"broker_eligible": True, "data_quality_factor": 1.0}},
    )
    assert incomplete["core_symbols"] == []
    assert "account-economics" in incomplete["core_selection_explanation"]

    complete_metadata = {
        symbol: {
            "broker_eligible": True, "data_quality_factor": 1.0,
            "out_of_sample_sample": 100, "forward_sample": 100,
            "out_of_sample_score": 90.0, "forward_demo_score": 90.0,
            "stability_score": 90.0, "execution_quality_score": 90.0,
            "account_size_suitability_score": 90.0, "portfolio_contribution_score": 90.0,
            "execution_reliability": 0.99,
        }
        for symbol in ("Boom 500 Index", "Boom 100 Index")
    }
    complete = governance.rank_instrument_specialization(
        ["Boom 500 Index", "Boom 100 Index"], outcomes, instrument_metadata=complete_metadata,
    )
    assert len(complete["core_symbols"]) == 1
    assert complete["core_symbols"][0] in {"Boom 500 Index", "Boom 100 Index"}


def test_core_portfolio_selection_rejects_highly_correlated_candidates():
    from knowledge.portfolio import pearson_correlation, select_diversified_core

    assert pearson_correlation([1, 2, 3], [2, 4, 6]) > 0.99
    rankings = [
        {"instrument": "A", "role": "CORE", "specialization": {"adjusted_score": 90}},
        {"instrument": "B", "role": "CORE", "specialization": {"adjusted_score": 85}},
        {"instrument": "C", "role": "CORE", "specialization": {"adjusted_score": 80}},
    ]
    selection = select_diversified_core(
        rankings,
        return_series={"A": [1, 2, 3], "B": [2, 4, 6], "C": [3, 1, 2]},
        max_core_instruments=3,
        correlation_threshold=0.85,
    )
    assert selection.selected == ("A", "C")
    assert selection.rejected[0]["instrument"] == "B"


def test_feature_importance_records_classify_unknown_redundant_and_supported():
    from analysis.research_governance import ResearchGovernance
    from config import TradeSettings

    governance = ResearchGovernance(TradeSettings.defaults())
    rows = []
    for index in range(20):
        rows.append({
            "symbol": "Boom 500 Index", "strategy_id": "s1", "regime": "TRENDING", "timeframe": "M15",
            "pnl_r": 0.8 if index < 10 else 0.1,
            "features": {"displacement": index < 10, "redundant_feature": index < 5 or 10 <= index < 15},
        })
    records = governance.feature_importance_records(rows)
    by_feature = {row["feature_name"]: row for row in records}
    assert by_feature["displacement"]["evidence_state"] == "SUPPORTED"
    assert by_feature["redundant_feature"]["evidence_state"] == "REDUNDANT"

    sparse = governance.feature_importance_records(rows[:6])
    assert all(row["evidence_state"] == "INSUFFICIENT_EVIDENCE" for row in sparse)
