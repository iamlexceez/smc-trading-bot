"""Setup validator for Setup Intelligence V2."""
from __future__ import annotations

from analysis.setup_intelligence.setup_record import TradeSetup


def validate_setup(setup: TradeSetup, *, min_rr: float = 1.5) -> tuple[bool, str]:
    if setup.risk_distance <= 0 or setup.reward_distance <= 0:
        return False, "Invalid risk or reward distance"
    if min_rr > 0 and setup.actual_rr < min_rr:
        return False, f"Actual RR 1:{setup.actual_rr:.2f} below authoritative minimum 1:{min_rr:.2f}"
    return True, "Valid"
