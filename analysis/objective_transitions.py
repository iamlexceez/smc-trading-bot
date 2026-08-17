"""Explicit objective-phase transition matrix and cross-subsystem validation."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional


@dataclass(frozen=True)
class PhaseTransition:
    from_phase: str
    to_phase: str
    condition: str
    action: str
    impact_on_trading: str
    impact_on_position_management: str
    impact_on_learning: str


class ObjectiveTransitionMatrix:
    """Governs the deterministic transitions between objective phases.
    
    Phases are economic states, not strategy states. Transitions must preserve
    evidence, strategy, and instrument DNA unless explicitly designed.
    """
    
    TRANSITIONS = {
        ("ACCUMULATION", "ACCUMULATION"): PhaseTransition(
            "ACCUMULATION", "ACCUMULATION", "Normal operation", "Continue",
            "Enabled", "Active", "Active"
        ),
        ("ACCUMULATION", "PHASE_COMPLETE"): PhaseTransition(
            "ACCUMULATION", "PHASE_COMPLETE", "Target reached", "Lock profits",
            "Paused for new entries", "Active (protection)", "Active"
        ),
        ("PHASE_COMPLETE", "ACCUMULATION"): PhaseTransition(
            "PHASE_COMPLETE", "ACCUMULATION", "Next phase started", "Resume scanning",
            "Enabled", "Active", "Active"
        ),
        ("ACCUMULATION", "DRAWDOWN_PROTECTION"): PhaseTransition(
            "ACCUMULATION", "DRAWDOWN_PROTECTION", "Drawdown limit reached", "Reduce risk",
            "Restricted", "Aggressive protection", "Active"
        ),
        ("DRAWDOWN_PROTECTION", "ACCUMULATION"): PhaseTransition(
            "DRAWDOWN_PROTECTION", "ACCUMULATION", "Recovery achieved", "Restore risk",
            "Enabled", "Active", "Active"
        ),
        ("ACCUMULATION", "CAPITAL_EXHAUSTED"): PhaseTransition(
            "ACCUMULATION", "CAPITAL_EXHAUSTED", "Starting capital lost", "Stop all",
            "Halted", "Close remaining", "Suspended"
        ),
    }

    @classmethod
    def get_transition(cls, from_phase: str, to_phase: str) -> Optional[PhaseTransition]:
        return cls.TRANSITIONS.get((from_phase.upper(), to_phase.upper()))

    @classmethod
    def validate_transition(cls, from_phase: str, to_phase: str, context: dict[str, Any]) -> dict[str, Any]:
        """Verify if a transition is valid under current broker and portfolio context."""
        transition = cls.get_transition(from_phase, to_phase)
        if not transition:
            return {"valid": False, "reason": f"Unknown transition: {from_phase} -> {to_phase}"}
        
        # Cross-subsystem checks
        checks = {
            "broker_connected": context.get("broker_connected", False),
            "account_verified": context.get("account_verified", False),
            "equity_sufficient": context.get("equity", 0) > 0,
        }
        
        if not all(checks.values()):
            failed = [k for k, v in checks.items() if not v]
            return {"valid": False, "reason": f"System state invalid for transition: {', '.join(failed)}", "checks": checks}
            
        return {
            "valid": True,
            "transition": transition,
            "checks": checks,
            "action_required": transition.action
        }


def audit_objective_state_isolation(current_phase: str, requested_action: str) -> bool:
    """Ensure objective phases do not accidentally reset intelligence components."""
    prohibited_resets = {"evidence", "strategy_dna", "instrument_dna", "learning_outcomes"}
    if requested_action.lower() in prohibited_resets:
        return False
    return True
