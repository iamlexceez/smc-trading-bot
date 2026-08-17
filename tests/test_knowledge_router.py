from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import pytest


def test_economic_regime_classification():
    from knowledge.economics import classify_economic_regime
    from knowledge.economics import geometric_growth

    assert classify_economic_regime(10).name == "SURVIVAL"
    assert classify_economic_regime(30).name == "CONTROLLED_GROWTH"
    assert classify_economic_regime(75).name == "GROWTH"
    assert classify_economic_regime(150).name == "EXPANSION"
    assert geometric_growth([0.1, -0.05]) == 1.045


def test_instrument_roles_distinguish_unknown_negative_and_core_evidence():
    from knowledge.instruments import classify_instrument

    assert classify_instrument(
        "Boom 500 Index", broker_eligible=True, evidence={}, minimum_sample_size=30,
        max_manageable_drawdown_r=2.0, minimum_execution_reliability=0.95,
    ).role == "EXPLORATORY"
    assert classify_instrument(
        "Boom 500 Index", broker_eligible=True,
        evidence={"sample_size": 30, "expectancy_r": -0.1}, minimum_sample_size=30,
        max_manageable_drawdown_r=2.0, minimum_execution_reliability=0.95,
    ).role == "UNDER_REVIEW"
    assert classify_instrument(
        "Boom 500 Index", broker_eligible=True,
        evidence={"sample_size": 50, "expectancy_r": 0.3, "max_drawdown_r": 1.0, "execution_reliability": 0.99},
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
    assert evidence_tier(0) == "UNKNOWN"
    assert evidence_tier(5) == "UNKNOWN"
    assert evidence_tier(15) == "EARLY"
    assert evidence_tier(35) == "DEVELOPING"
    assert evidence_tier(100) == "STRONG"

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
    assert weak.evidence_tier == "UNKNOWN"
    assert "opportunity" not in " ".join(mature.reasons).lower()


def test_specialization_governance_requires_complete_core_evidence_and_does_not_pad_slots():
    from knowledge.portfolio import select_diversified_core

    rankings = [
        {"instrument": "A", "adjusted_score": 90, "role": "CORE", "role_reason": "Pass"},
        {"instrument": "B", "adjusted_score": 85, "role": "CORE", "role_reason": "Pass"},
        {"instrument": "C", "adjusted_score": 80, "role": "RESEARCH", "role_reason": "Fail"},
    ]
    core = select_diversified_core(
        rankings, return_series={}, max_core_instruments=5, correlation_threshold=0.7
    )
    assert len(core.selected) == 2
    assert "A" in core.selected
    assert "B" in core.selected
    assert "C" not in core.selected
