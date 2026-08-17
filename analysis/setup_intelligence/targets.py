"""Take profit and target engine for Setup Intelligence V2 with 1.5 min / 2.0 preferred RR authority."""
from __future__ import annotations


def calculate_target(direction: str, entry_price: float, stop_loss: float, preferred_rr: float = 2.0) -> float:
    risk = abs(entry_price - stop_loss)
    reward = risk * preferred_rr
    if direction.upper() == "BUY":
        return entry_price + reward
    else:
        return entry_price - reward
