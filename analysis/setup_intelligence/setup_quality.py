"""Setup quality scoring for Setup Intelligence V2."""
from __future__ import annotations

from analysis.setup_intelligence.setup_record import TradeSetup


def calculate_quality(setup: TradeSetup) -> float:
    base = 50.0
    if setup.actual_rr >= 2.0:
        base += 20.0
    if setup.liquidity_sweep.get("swept"):
        base += 15.0
    if setup.poi_type:
        base += 15.0
    return min(100.0, base)
