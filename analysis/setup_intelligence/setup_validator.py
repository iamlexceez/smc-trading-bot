"""Setup validator for Setup Intelligence V2.

V2 establishes whether the setup has executable geometry. RR policy remains a
separate downstream governance concern so DEMO exploration can observe lower-RR
hypotheses without corrupting the causal setup record. LIVE execution still
uses the authoritative decision gate and broker validation.
"""
from __future__ import annotations

from analysis.setup_intelligence.setup_record import TradeSetup


def validate_setup(setup: TradeSetup, *, min_rr: float = 1.5) -> tuple[bool, str]:
    """Validate causal geometry without turning RR into setup construction."""
    if setup.risk_distance <= 0 or setup.reward_distance <= 0:
        return False, "Invalid risk or reward distance"
    if min_rr > 0 and setup.actual_rr < min_rr:
        return True, (
            f"Valid geometry; actual RR 1:{setup.actual_rr:.2f} is below policy floor "
            f"1:{min_rr:.2f} and is deferred to downstream execution governance"
        )
    return True, "Valid"
