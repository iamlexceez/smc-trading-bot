from __future__ import annotations

from datetime import datetime, timedelta, timezone
import math
from types import SimpleNamespace

from analysis.market_state import current_r, market_state_from_quote
from analysis.opportunity import rank_opportunities
from executors.base import Position


def test_market_state_marks_old_quote_stale() -> None:
    observed = datetime.now(timezone.utc) - timedelta(seconds=31)
    state = market_state_from_quote("TEST", 100.0, 100.2, observed_at=observed, stale_after_seconds=30)
    assert state.mid == 100.1
    assert math.isclose(state.spread, 0.2, rel_tol=1e-9)
    assert state.stale is True


def test_market_state_accepts_fresh_quote() -> None:
    state = market_state_from_quote("TEST", 100.0, 100.2, stale_after_seconds=30)
    assert state.stale is False
    assert state.bid == 100.0
    assert state.ask == 100.2


def test_current_r_uses_side_aware_live_price() -> None:
    buy = Position(1, "TEST", "BUY", 1.0, 100.0, 99.0, 102.0)
    sell = Position(2, "TEST", "SELL", 1.0, 100.0, 101.0, 98.0)
    assert current_r(buy, 101.0) == 1.0
    assert current_r(sell, 99.0) == 1.0


def test_ranking_exposes_uncertainty_and_prefers_evidence() -> None:
    def candidate(symbol: str, score: float) -> SimpleNamespace:
        return SimpleNamespace(
            symbol=symbol, score=score, selected_strategy="continuation", strategy_score=score,
            strategy_evidence={}, expected_value_r=1.0, entry_price=100.0,
            stop_loss=99.0, take_profit=102.0, direction="BUY", timeframe="M15",
            htf_bias=["BULLISH"], layering_suitable=False,
        )

    ranked = rank_opportunities(
        [candidate("A", 80), candidate("B", 80)], profiles={},
        contexts={"A": {"regime": "TRENDING", "adx": 30, "atr_ratio": 1.0, "momentum": 0.4},
                  "B": {"regime": "TRENDING", "adx": 30, "atr_ratio": 1.0, "momentum": 0.4}},
        historical={"A": {"sample_size": 40, "expectancy_r": 0.8},
                    "B": {"sample_size": 0}},
    )
    assert ranked[0].symbol == "A"
    assert ranked[0].details["uncertainty"] in {"MEDIUM", "LOW"}
    assert ranked[1].details["uncertainty"] in {"HIGH", "INSUFFICIENT_EVIDENCE"}
    assert ranked[0].details["why_selected"]
    assert ranked[1].details["why_not_selected"]


class _QuoteExecutor:
    async def get_symbol_price(self, symbol: str) -> tuple[float, float]:
        assert symbol == "TEST"
        return 101.0, 101.2

    async def get_open_positions(self) -> list[Position]:
        return [Position(7, "TEST", "BUY", 1.0, 100.0, 99.0, 103.0, profit=12.5)]


def test_live_position_enrichment_uses_fresh_side_price() -> None:
    import asyncio
    from analysis.market_state import read_live_positions

    rows = asyncio.run(read_live_positions(_QuoteExecutor()))
    assert len(rows) == 1
    assert rows[0].current_price == 101.0
    assert rows[0].current_r == 1.0
    assert rows[0].unrealized_pnl == 12.5
    assert rows[0].market.stale is False


def test_insufficient_evidence_can_be_controlled_forward_demo_exploration() -> None:
    from analysis.decision_gates import evaluate_trading_gate

    decision = evaluate_trading_gate(
        setup_valid=True,
        broker_symbol_valid=True,
        valid_market_data=True,
        objective_permits_exposure=True,
        evidence={"sample_size": 0, "evidence_classification": "INSUFFICIENT", "confidence_classification": "LOW"},
        champion_governed=False,
        forward_demo_experiment_allowed=False,
        portfolio_approved=True,
        structural_conflict=False,
        required_htf_context_available=True,
        setup_quality=84.5,
        exploratory_threshold=80.0,
        strategy_quality=84.0,
        strategy_threshold=80.0,
        demo_mode=True,
        experiment_id=None,
        exploration_authorized=True,
        strategy_status="UNVALIDATED",
    )
    assert decision.trading_decision == "CONTROLLED_FORWARD_DEMO"
    assert decision.final_state == "EXPLORATORY_DEMO"
    assert decision.evidence_classification == "INSUFFICIENT"
    assert decision.confidence_classification == "LOW"
    assert decision.strategy_status == "UNVALIDATED"
    assert decision.hard_gate_results == {
        "broker_symbol": True, "market_data": True, "setup_geometry": True,
        "objective": True, "portfolio": True, "required_htf_context": True,
        "risk_policy": True,
    }


def test_negative_evidence_is_rejected_even_when_setup_is_strong() -> None:
    from analysis.decision_gates import evaluate_trading_gate

    decision = evaluate_trading_gate(
        setup_valid=True, broker_symbol_valid=True, valid_market_data=True,
        objective_permits_exposure=True,
        evidence={"evidence_classification": "NEGATIVE", "confidence_classification": "HIGH"},
        champion_governed=True, portfolio_approved=True,
        required_htf_context_available=True,
    )
    assert decision.evidence_classification == "NEGATIVE"
    assert decision.trading_decision == "TRADE_REJECTED"
    assert decision.final_state == "REJECTED"


def test_evidence_states_do_not_collapse_unknown_into_negative() -> None:
    from analysis.decision_gates import classify_evidence

    assert classify_evidence({"evidence_classification": "INSUFFICIENT"}) == "INSUFFICIENT"
    assert classify_evidence({"evidence_classification": "EMERGING"}) == "EMERGING"
    assert classify_evidence({"evidence_classification": "VALIDATED"}) == "VALIDATED"
    assert classify_evidence({"evidence_classification": "INVALIDATED"}) == "INVALIDATED"


def test_weak_setup_with_insufficient_evidence_is_rejected() -> None:
    from analysis.decision_gates import evaluate_trading_gate

    decision = evaluate_trading_gate(
        setup_valid=True, broker_symbol_valid=True, valid_market_data=True,
        objective_permits_exposure=True,
        evidence={"evidence_classification": "INSUFFICIENT"},
        champion_governed=False, portfolio_approved=True,
        required_htf_context_available=True,
        setup_quality=61.0, exploratory_threshold=80.0,
        strategy_quality=84.0, strategy_threshold=80.0,
        demo_mode=True, exploration_authorized=True,
    )
    assert decision.trading_decision == "NO_TRADE"
    assert decision.final_state == "NO_TRADE"
    assert "below exploration threshold" in decision.reason


def test_insufficient_evidence_does_not_override_hard_gate() -> None:
    from analysis.decision_gates import evaluate_trading_gate

    decision = evaluate_trading_gate(
        setup_valid=True,
        broker_symbol_valid=True,
        valid_market_data=True,
        objective_permits_exposure=False,
        evidence={"sample_size": 0, "evidence_classification": "INSUFFICIENT"},
        champion_governed=False,
        forward_demo_experiment_allowed=True,
        portfolio_approved=True,
        structural_conflict=False,
        required_htf_context_available=True,
        setup_quality=95.0,
        exploratory_threshold=80.0,
        demo_mode=True,
        experiment_id=None,
        exploration_authorized=True,
    )
    assert decision.final_state == "EXECUTION_BLOCKED"
    assert decision.trading_decision == "EXECUTION_BLOCKED"
    assert "OBJECTIVE_INCOMPATIBLE" in decision.reason_codes


def test_conflicted_top_down_context_is_explicitly_waiting() -> None:
    from analysis.decision_gates import evaluate_trading_gate

    decision = evaluate_trading_gate(
        setup_valid=True,
        broker_symbol_valid=True,
        valid_market_data=True,
        objective_permits_exposure=True,
        evidence={"evidence_classification": "INSUFFICIENT"},
        champion_governed=False,
        forward_demo_experiment_allowed=True,
        portfolio_approved=True,
        structural_conflict=True,
        required_htf_context_available=True,
        setup_quality=90.0,
        exploration_authorized=True,
        exploratory_threshold=80.0,
        demo_mode=True,
        experiment_id=17,
    )
    assert decision.final_state == "WAITING_FOR_CONFIRMATION"
    assert decision.trading_decision == "DEFERRED"


def test_forward_demo_challenger_is_allowed_to_trade_in_isolated_demo() -> None:
    from analysis.decision_gates import evaluate_trading_gate

    decision = evaluate_trading_gate(
        setup_valid=True,
        broker_symbol_valid=True,
        valid_market_data=True,
        objective_permits_exposure=True,
        evidence={"evidence_classification": "STRONG", "confidence_classification": "HIGH"},
        champion_governed=False,
        forward_demo_experiment_allowed=True,
        portfolio_approved=True,
        structural_conflict=False,
        required_htf_context_available=True,
    )
    assert decision.trading_decision == "TRADE_APPROVED"
    assert decision.final_state == "EXECUTION_APPROVED"


def test_challenger_without_isolated_demo_authority_is_not_a_hidden_execution_block() -> None:
    from analysis.decision_gates import evaluate_trading_gate

    decision = evaluate_trading_gate(
        setup_valid=True,
        broker_symbol_valid=True,
        valid_market_data=True,
        objective_permits_exposure=True,
        evidence={"evidence_classification": "STRONG", "confidence_classification": "HIGH"},
        champion_governed=False,
        forward_demo_experiment_allowed=False,
        strategy_status="CHALLENGER",
        portfolio_approved=True,
        structural_conflict=False,
        required_htf_context_available=True,
    )
    assert decision.trading_decision == "TRADE_APPROVED"
    assert decision.final_state == "EXECUTION_APPROVED"
    assert decision.strategy_status == "CHALLENGER"
    assert not decision.failures


def test_execution_classes_and_gated_ranking_are_explicit() -> None:
    from analysis.decision_gates import evaluate_trading_gate
    from analysis.opportunity import rank_opportunities, score_band

    exploration = evaluate_trading_gate(
        setup_valid=True, broker_symbol_valid=True, valid_market_data=True,
        objective_permits_exposure=True, evidence={"evidence_classification": "INSUFFICIENT"},
        champion_governed=False, portfolio_approved=True, required_htf_context_available=True,
        setup_quality=85.0, exploratory_threshold=80.0, strategy_quality=85.0,
        strategy_threshold=80.0, demo_mode=True, exploration_authorized=True,
    )
    assert exploration.execution_class == "EXPLORATION"

    proven = evaluate_trading_gate(
        setup_valid=True, broker_symbol_valid=True, valid_market_data=True,
        objective_permits_exposure=True, evidence={"evidence_classification": "VALIDATED", "confidence_classification": "HIGH", "sample_size": 50, "expectancy_r": 0.4},
        champion_governed=True, portfolio_approved=True, required_htf_context_available=True,
    )
    assert proven.execution_class == "PROVEN"

    blocked = evaluate_trading_gate(
        setup_valid=False, broker_symbol_valid=True, valid_market_data=True,
        objective_permits_exposure=True, evidence={"evidence_classification": "VALIDATED"},
        champion_governed=True, portfolio_approved=True, required_htf_context_available=True,
    )
    assert blocked.execution_class == "RESEARCH_ONLY"
    assert score_band(59.1) == "MARGINAL"
    assert score_band(74.0) == "GOOD"

    validation = SimpleNamespace(
        minimum_rr=1.5, rr_filter_enabled=True, target_source="policy_rr_target",
        structural_target=0.0, structural_rr=0.0, target_conflict=False,
        target_candidates=[],
    )
    candidate = SimpleNamespace(
        symbol="PolicyTarget", score=95.0, selected_strategy="bos_choch_continuation",
        strategy_score=95.0, strategy_evidence={"sample_size": 50, "expectancy_r": 0.5, "confidence": "VALIDATED"},
        expected_value_r=0.5, entry_price=100.0, stop_loss=99.0, take_profit=101.0,
        direction="BUY", timeframe="M15", htf_bias=["BULLISH"], htf_context=[],
        target_source="policy_rr_target", validation=validation, layering_suitable=False,
        experimental_policy={}, research_decision="RESEARCH_ACCEPTED", trading_decision="DEFERRED",
    )
    ranked = rank_opportunities(
        [candidate], profiles={},
        contexts={"PolicyTarget": {"regime": "TRENDING", "adx": 30.0, "atr_ratio": 1.0, "momentum": 0.4}},
        historical={}, capacity_context={"new_exposure_allowed": True, "exploration_enabled": True},
    )
    assert ranked[0].execution_class == "RESEARCH_ONLY"
    assert "legitimate" in ranked[0].details["execution_class_reason"]


def test_htf_conflict_and_near_tie_are_visible() -> None:
    from analysis.opportunity import rank_opportunities

    def candidate(symbol: str) -> SimpleNamespace:
        validation = SimpleNamespace(
            minimum_rr=2.0, rr_filter_enabled=True, target_source="liquidity:swing_high",
            structural_target=103.0, structural_rr=3.0, target_conflict=False,
            target_candidates=[{"level": 103.0, "rr_ratio": 3.0}],
        )
        return SimpleNamespace(
            symbol=symbol, score=80.0, selected_strategy="bos_choch_continuation", strategy_score=85.0,
            strategy_evidence={"sample_size": 50, "expectancy_r": 0.4, "confidence": "VALIDATED"},
            expected_value_r=0.4, entry_price=100.0, stop_loss=99.0, take_profit=103.0,
            direction="BUY", timeframe="M15", htf_bias=["BULLISH", "BEARISH"], htf_context=[{"timeframe": "H1", "bias": "BEARISH"}, {"timeframe": "H4", "bias": "BULLISH"}],
            htf_bias_status="CONFLICTED", target_source="liquidity:swing_high", validation=validation,
            layering_suitable=False, experimental_policy={}, research_decision="RESEARCH_ACCEPTED", trading_decision="DEFERRED",
        )

    ranked = rank_opportunities(
        [candidate("A"), candidate("B")], profiles={},
        contexts={"A": {"regime": "TRENDING", "adx": 30.0, "atr_ratio": 1.0, "momentum": 0.4}, "B": {"regime": "TRENDING", "adx": 30.0, "atr_ratio": 1.0, "momentum": 0.4}},
        historical={}, capacity_context={"new_exposure_allowed": True, "exploration_enabled": True, "ranking_tie_threshold": 2.0},
    )
    assert all(item.execution_class == "RESEARCH_ONLY" for item in ranked)
    assert ranked[0].details["htf_relationship"] == "CONFLICTED"
    assert ranked[0].details["ranking_label"] == "RANKING_TIE"
    assert ranked[1].details["ranking_label"] == "RANKING_TIE"
    assert ranked[0].details["best_executable_symbol"] is None
    assert ranked[0].details["best_research_symbol"] in {"A", "B"}
