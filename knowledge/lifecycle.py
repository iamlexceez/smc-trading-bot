"""Evidence-governed Core/Challenger lifecycle decisions.

These functions describe promotion, demotion, review, and quarantine. They never
select an order or bypass broker, risk, portfolio, or execution gates.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any


LIFECYCLE_ROLES = ("RESEARCH", "CANDIDATE", "CHALLENGER", "CORE", "DECLINING", "REVIEW", "QUARANTINED")


@dataclass(frozen=True)
class LifecycleDecision:
    previous_role: str
    new_role: str
    reason: str
    evidence_sufficient: bool
    replacement_eligible: bool = False

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _number(value: Any) -> float | None:
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _positive(value: Any) -> bool:
    number = _number(value)
    return number is not None and number > 0.0


def _complete_core_evidence(evidence: dict[str, Any], *, minimum_sample_size: int, score_threshold: float) -> bool:
    return (
        int(evidence.get("sample_size") or 0) >= max(1, int(minimum_sample_size))
        and _number(evidence.get("adjusted_score")) is not None
        and float(evidence.get("adjusted_score")) >= float(score_threshold)
        and _positive(evidence.get("out_of_sample_expectancy_r"))
        and _positive(evidence.get("forward_expectancy_r"))
        and _positive(evidence.get("expectancy_r"))
        and _number(evidence.get("max_drawdown_r")) is not None
        and _number(evidence.get("stability_score")) is not None
        and _number(evidence.get("execution_reliability")) is not None
        and _number(evidence.get("account_size_suitability_score")) is not None
        and _number(evidence.get("portfolio_contribution_score")) is not None
    )


def challenger_replacement(
    challenger: dict[str, Any],
    core: dict[str, Any],
    *,
    minimum_sample_size: int = 50,
    score_threshold: float = 75.0,
    minimum_expectancy_improvement: float = 0.05,
    max_drawdown_worsening: float = 0.10,
) -> LifecycleDecision:
    """Require meaningful multi-factor superiority before replacing Core."""
    challenger_score = _number(challenger.get("adjusted_score"))
    core_score = _number(core.get("adjusted_score"))
    challenger_expectancy = _number(challenger.get("forward_expectancy_r", challenger.get("expectancy_r")))
    core_expectancy = _number(core.get("forward_expectancy_r", core.get("expectancy_r")))
    challenger_drawdown = _number(challenger.get("max_drawdown_r"))
    core_drawdown = _number(core.get("max_drawdown_r"))
    challenger_reliability = _number(challenger.get("execution_reliability"))
    core_reliability = _number(core.get("execution_reliability"))
    complete = _complete_core_evidence(challenger, minimum_sample_size=minimum_sample_size, score_threshold=score_threshold)
    improvement = (
        complete
        and challenger_expectancy is not None and core_expectancy is not None
        and challenger_expectancy >= core_expectancy + float(minimum_expectancy_improvement)
        and (challenger_drawdown is None or core_drawdown is None or challenger_drawdown <= core_drawdown + float(max_drawdown_worsening))
        and (challenger_reliability is None or core_reliability is None or challenger_reliability >= core_reliability)
    )
    if improvement:
        return LifecycleDecision("CHALLENGER", "CORE", "Challenger demonstrates meaningful forward/OOS, risk, stability, execution, economics, and portfolio superiority.", True, True)
    missing = []
    if not complete:
        missing.append("complete Core evidence")
    if challenger_score is None or core_score is None:
        missing.append("comparable adjusted score")
    if challenger_expectancy is None or core_expectancy is None or challenger_expectancy < core_expectancy + float(minimum_expectancy_improvement):
        missing.append("meaningful expectancy improvement")
    return LifecycleDecision("CHALLENGER", "CHALLENGER", "Replacement deferred: " + ", ".join(dict.fromkeys(missing)) + ".", complete, False)


def lifecycle_transition(
    previous_role: str,
    evidence: dict[str, Any],
    *,
    minimum_sample_size: int = 50,
    score_threshold: float = 75.0,
    review_cycles: int = 0,
) -> LifecycleDecision:
    """Return a deterministic evidence-based lifecycle transition."""
    previous = str(previous_role or "RESEARCH").upper()
    if not bool(evidence.get("broker_eligible", True)):
        return LifecycleDecision(previous, "QUARANTINED", "Broker eligibility is not verified.", False)
    if bool(evidence.get("execution_reliability_issue")) or bool(evidence.get("persistent_instability")):
        if previous in {"DECLINING", "REVIEW"} and int(review_cycles or 0) >= 1:
            return LifecycleDecision(previous, "QUARANTINED", "Execution or stability deterioration persisted through review.", False)
        if previous == "CORE":
            return LifecycleDecision(previous, "DECLINING", "Core evidence shows a governed execution or stability deterioration.", False)
    if _number(evidence.get("rolling_expectancy_r")) is not None and _number(evidence.get("rolling_expectancy_r")) <= 0:
        if previous == "CORE":
            return LifecycleDecision(previous, "DECLINING", "Rolling expectancy is non-positive; Core status is under review.", False)
        if previous == "DECLINING" and int(review_cycles or 0) >= 1:
            return LifecycleDecision(previous, "REVIEW", "Negative rolling expectancy persisted into review.", False)
    if previous in {"DECLINING", "REVIEW"} and _complete_core_evidence(evidence, minimum_sample_size=minimum_sample_size, score_threshold=score_threshold):
        return LifecycleDecision(previous, "CORE", "Evidence recovered across performance, stability, execution, economics, and portfolio contribution.", True)
    if previous == "RESEARCH" and int(evidence.get("sample_size") or 0) > 0:
        return LifecycleDecision(previous, "CANDIDATE", "Initial realized evidence exists; candidate review is required.", False)
    if previous == "CANDIDATE" and int(evidence.get("sample_size") or 0) >= max(5, int(minimum_sample_size // 5)):
        return LifecycleDecision(previous, "CHALLENGER", "Evidence depth supports controlled Challenger evaluation.", False)
    if previous == "CHALLENGER" and _complete_core_evidence(evidence, minimum_sample_size=minimum_sample_size, score_threshold=score_threshold):
        return LifecycleDecision(previous, "CORE", "Core promotion criteria are satisfied; portfolio replacement review remains separate.", True)
    return LifecycleDecision(previous, previous if previous in LIFECYCLE_ROLES else "RESEARCH", "No governed lifecycle transition is supported by current evidence.", False)


__all__ = ["LIFECYCLE_ROLES", "LifecycleDecision", "challenger_replacement", "lifecycle_transition"]
