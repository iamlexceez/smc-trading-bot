"""Setup validator for Setup Intelligence V2."""
from __future__ import annotations

from analysis.setup_intelligence.setup_record import TradeSetup


def validate_setup(setup: TradeSetup) -> tuple[bool, str]:
    if setup.risk_distance <= 0 or setup.reward_distance <= 0:
        return False, "Invalid risk or reward distance"
    if setup.actual_rr < 1.5:
        return False, f"Actual RR 1:{setup.actual_rr:.2f} below authoritative minimum 1:1.50"
    return True, "Valid"
