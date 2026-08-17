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
