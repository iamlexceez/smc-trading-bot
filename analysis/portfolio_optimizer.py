"""Portfolio Optimizer for Trading Intelligence V2."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional


@dataclass(frozen=True)
class PortfolioImpact:
    expected_value_change: float
    risk_change_pct: float
    correlation_change: float
    margin_change_usd: float
    approved: bool
    reason: str


class PortfolioOptimizer:
    """Evaluates the marginal value of adding a new trade to the existing portfolio."""
    
    @staticmethod
    def evaluate_impact(
        current_portfolio: dict[str, Any],
        candidate_trade: dict[str, Any],
        max_total_risk_pct: float = 3.0,
        max_correlation: float = 0.85
    ) -> PortfolioImpact:
        existing_risk = float(current_portfolio.get("total_risk_pct", 0.0))
        new_risk = float(candidate_trade.get("risk_pct", 0.0))
        
        total_risk = existing_risk + new_risk
        if total_risk > max_total_risk_pct:
            return PortfolioImpact(
                0.0, new_risk, 0.0, 0.0, False,
                f"Total risk {total_risk:.2f}% exceeds limit {max_total_risk_pct}%"
            )
            
        # Check for instrument concentration and correlation
        # (Simplified: check if instrument already exists)
        existing_instruments = current_portfolio.get("instruments", [])
        if candidate_trade["instrument"] in existing_instruments:
            return PortfolioImpact(
                0.0, new_risk, 1.0, 0.0, False,
                f"Concentration risk: {candidate_trade['instrument']} already in portfolio"
            )
            
        return PortfolioImpact(
            candidate_trade.get("ev_r", 0.0),
            new_risk,
            0.1, # Estimated correlation change
            candidate_trade.get("margin_usd", 0.0),
            True,
            "Approved: Positive marginal portfolio value"
        )
