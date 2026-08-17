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
