"""Small Account Optimization Engine."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class AccountEconomics:
    balance: float
    equity: float
    free_margin: float
    minimum_volume: float
    volume_step: float
    contract_size: float
    margin_required: float
    stop_distance: float
    risk_usd: float
    expected_value_r: float
    reward_usd: float
    correlation_penalty: float
    broker_constraints: list[str]


def evaluate_small_account_efficiency(
    economics: AccountEconomics,
    max_drawdown_risk_pct: float = 5.0
) -> dict[str, Any]:
    """Evaluate whether a trade is the best executable use of available capital."""
    
    if economics.equity <= 0 or economics.free_margin <= 0:
        return {"executable": False, "reason": "Insufficient equity or free margin", "efficiency_score": 0.0}
        
    if economics.margin_required > economics.free_margin:
        return {"executable": False, "reason": "Required margin exceeds free margin", "efficiency_score": 0.0}
        
    risk_pct = (economics.risk_usd / economics.equity) * 100
    if risk_pct > max_drawdown_risk_pct:
        return {"executable": False, "reason": f"Risk {risk_pct:.1f}% exceeds maximum {max_drawdown_risk_pct}%", "efficiency_score": 0.0}
        
    if economics.expected_value_r <= 0:
        return {"executable": False, "reason": "Negative or zero expected value", "efficiency_score": 0.0}
        
    # Capital Efficiency = ExpectedValueContribution / RequiredCapitalConstraint
    # Adjusted by correlation penalty to prevent concentrated exposure
    capital_utilization = economics.margin_required / economics.equity
    if capital_utilization == 0:
        capital_utilization = 0.01  # Prevent division by zero
        
    efficiency_score = (economics.expected_value_r * economics.reward_usd) / (economics.margin_required + economics.risk_usd)
    adjusted_score = efficiency_score * (1.0 - economics.correlation_penalty)
    
    return {
        "executable": True,
        "reason": "Meets small account economic constraints",
        "efficiency_score": adjusted_score,
        "metrics": {
            "risk_pct": risk_pct,
            "capital_utilization": capital_utilization,
            "raw_efficiency": efficiency_score,
            "correlation_penalty": economics.correlation_penalty
        }
    }
