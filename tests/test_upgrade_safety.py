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


def test_challenger_without_isolated_demo_authority_is_rejected() -> None:
    from analysis.decision_gates import evaluate_trading_gate

    decision = evaluate_trading_gate(
        setup_valid=True,
        broker_symbol_valid=True,
        valid_market_data=True,
        objective_permits_exposure=True,
        evidence={"evidence_classification": "STRONG", "confidence_classification": "HIGH"},
        champion_governed=False,
        forward_demo_experiment_allowed=False,
        portfolio_approved=True,
        structural_conflict=False,
        required_htf_context_available=True,
    )
    assert decision.trading_decision == "OBJECTIVE_INELIGIBLE"
    assert "Champion/challenger governance" in decision.failures
