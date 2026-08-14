"""Adaptive milestone planning for confirmed DEMO growth objectives.

Phase planning measures objective progress; it never selects a trade, risk amount,
or broker order. All policy and MT5 execution decisions remain in their existing
components.
"""

from __future__ import annotations

from dataclasses import dataclass
from math import ceil, log
from typing import Any, Mapping


@dataclass(frozen=True)
class ObjectivePhasePlan:
    """A broker-aware, objective-preserving sequence of milestone targets."""

    starting_equity: float
    target_equity: float
    phase_targets: tuple[float, ...]
    planning_basis: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return {
            "starting_equity": self.starting_equity,
            "target_equity": self.target_equity,
            "phase_targets": list(self.phase_targets),
            "planning_basis": dict(self.planning_basis),
        }


def _num(value: Any, fallback: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return fallback


def plan_objective_phases(
    *,
    starting_equity: float,
    target_equity: float,
    minimum_operating_capital: float = 0.0,
    historical_evidence: Mapping[str, Any] | None = None,
) -> ObjectivePhasePlan:
    """Create geometric, adaptive milestones that preserve the exact final target.

    The number of phases changes with the objective multiple, broker minimum
    operating capital, and sufficiently sized realized evidence. The plan is a
    progress-measurement structure, not a preselected exposure or sizing model.
    """
    start = _num(starting_equity)
    target = _num(target_equity)
    if start <= 0 or target <= start:
        raise ValueError("Objective phase planning requires a positive target above starting equity")

    evidence = dict(historical_evidence or {})
    multiple = target / start
    # The baseline produces readable geometric milestones while avoiding a
    # fixed dollar ladder. The count remains derived from the actual objective
    # multiple instead of being capped by a preset milestone limit.
    phase_count = max(2, int(ceil(log(multiple) / log(2.0))))
    operating = max(0.0, _num(minimum_operating_capital))
    resolution = start / operating if operating > 0 else None
    if resolution is not None and resolution < 3:
        phase_count += 2
    elif resolution is not None and resolution < 8:
        phase_count += 1

    sample_size = int(_num(evidence.get("sample_size")))
    expectancy = _num(evidence.get("expectancy_r"))
    drawdown = _num(evidence.get("max_drawdown_r"))
    # Realized evidence may refine milestone granularity only after a minimum
    # sample exists. It cannot itself change the user target or trade policy.
    if sample_size >= 12:
        if expectancy > 0 and drawdown <= 2:
            phase_count -= 1
        elif expectancy <= 0 or drawdown >= 4:
            phase_count += 1
    phase_count = max(2, phase_count)

    multiplier = multiple ** (1.0 / phase_count)
    targets: list[float] = []
    prior = start
    for number in range(1, phase_count + 1):
        milestone = target if number == phase_count else round(start * (multiplier ** number), 2)
        # Currency rounding must not create duplicate or regressive milestones.
        milestone = max(milestone, round(prior + 0.01, 2))
        targets.append(milestone)
        prior = milestone
    targets[-1] = target

    return ObjectivePhasePlan(
        starting_equity=start,
        target_equity=target,
        phase_targets=tuple(targets),
        planning_basis={
            "objective_multiple": multiple,
            "phase_count": phase_count,
            "geometric_multiplier": multiplier,
            "minimum_operating_capital": operating,
            "capital_resolution": resolution,
            "historical_sample_size": sample_size,
            "historical_expectancy_r": expectancy if sample_size else None,
            "historical_max_drawdown_r": drawdown if sample_size else None,
            "basis": "objective_scale_broker_operating_capital_and_completed_DEMO_evidence",
        },
    )


__all__ = ["ObjectivePhasePlan", "plan_objective_phases"]
