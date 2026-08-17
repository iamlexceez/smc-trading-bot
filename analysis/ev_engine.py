"""Cost-aware Expected Value Engine for Trading Intelligence V2."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional


@dataclass(frozen=True)
class EVResult:
    expected_value_r: float
    win_probability: float
    average_win_r: float
    average_loss_r: float
    costs_r: float
    sample_size: int
    confidence: str


class ExpectedValueEngine:
    """Calculates conditional expected value net of trading costs."""
    
    @staticmethod
    def calculate_ev(
        win_rate: float,
        avg_win_r: float,
        avg_loss_r: float,
        costs_r: float = 0.1, # Default estimated spread/slippage in R
        sample_size: int = 0
    ) -> EVResult:
        """Calculate EV = (Pwin * AvgWin) - (Ploss * AvgLoss) - Costs."""
        
        if sample_size < 10:
            confidence = "LOW"
        elif sample_size < 30:
            confidence = "MARGINAL"
        else:
            confidence = "GOOD"
            
        p_win = win_rate
        p_loss = 1.0 - p_win
        
        ev = (p_win * avg_win_r) - (p_loss * abs(avg_loss_r)) - costs_r
        
        return EVResult(
            expected_value_r=ev,
            win_probability=p_win,
            average_win_r=avg_win_r,
            average_loss_r=avg_loss_r,
            costs_r=costs_r,
            sample_size=sample_size,
            confidence=confidence
        )
