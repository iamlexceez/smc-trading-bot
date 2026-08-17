"""Invalidation and stop loss engine for Setup Intelligence V2."""
from __future__ import annotations


def calculate_stop_loss(direction: str, entry_price: float, poi_low: float, poi_high: float, atr: float) -> float:
    buffer = max(atr * 0.5, 0.0001)
    if direction.upper() == "BUY":
        return min(poi_low - buffer, entry_price - (atr * 2.0))
    else:
        return max(poi_high + buffer, entry_price + (atr * 2.0))
