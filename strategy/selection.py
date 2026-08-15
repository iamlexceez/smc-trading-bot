"""Evidence-aware selection among registry-defined strategy families.

This evaluator only ranks strategy applicability for an already observed setup.
It never creates an entry, bypasses broker checks, or promotes weak samples to
validated knowledge.
"""
from __future__ import annotations

from dataclasses import dataclass
import math
from typing import Any, Iterable

from strategy.registry import applicable_strategies


@dataclass(frozen=True)
class StrategyAssessment:
    identifier: str
    score: float
    confidence: str
    sample_size: int
    expectancy_r: float | None
    applicable: bool
    rationale: tuple[str, ...]
    evidence_stage: str = "exploration"
    transition_sample_size: int = 0
    transition_expectancy_r: float | None = None
    conservative_expectancy_r: float | None = None


def _finite(value: Any, default: float = 0.0) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    return number if math.isfinite(number) else default


def evidence_class(sample_size: int, expectancy_r: float | None) -> str:
    """Return explicit evidence states; depth is not a profitability claim."""
    del expectancy_r
    if sample_size < 3:
        return "INSUFFICIENT"
    if sample_size < 10:
        return "EMERGING"
    if sample_size < 20:
        return "PRELIMINARY"
    if sample_size < 50:
        return "VALIDATED"
    return "STRONG"


def evaluate_strategies(
    *, regime: str, timeframe: str, observed_features: Iterable[str], setup_quality: float,
    evidence_by_strategy: dict[str, dict[str, Any]],
    transition_evidence_by_strategy: dict[str, dict[str, Any]] | None = None,
) -> list[StrategyAssessment]:
    """Rank applicable strategies; scores are descriptive and not entry triggers.

    Components are explicit for future policy experimentation: current setup
    quality (60%), regime/timeframe/feature fit (25%), and capped completed
    context-specific expectancy evidence (15%).  A small sample contributes a
    bounded amount and retains an uncertainty label.
    """
    observed = {str(item) for item in observed_features}
    available = applicable_strategies(regime, timeframe, observed)
    assessments: list[StrategyAssessment] = []
    for definition in available:
        metric = dict(evidence_by_strategy.get(definition.identifier) or {})
        transition_metric = dict((transition_evidence_by_strategy or {}).get(definition.identifier) or {})
        sample = max(0, int(_finite(metric.get("sample_size"))))
        expectancy_raw = metric.get("expectancy_r")
        expectancy = _finite(expectancy_raw, 0.0) if sample else None
        transition_sample = max(0, int(_finite(transition_metric.get("sample_size"))))
        transition_expectancy_raw = transition_metric.get("expectancy_r")
        transition_expectancy = _finite(transition_expectancy_raw, 0.0) if transition_sample else None
        # Applicable registry definition is already regime/timeframe/feature-fit.
        fit = 1.0
        recent_expectancy = _finite(metric.get("recent_expectancy_r"), expectancy or 0.0) if sample else None
        ci_low_raw = metric.get("expectancy_ci95_low_r")
        ci_low = _finite(ci_low_raw, expectancy or 0.0) if ci_low_raw is not None and sample else None
        # A context may be ranked with completed evidence, but its score is
        # deliberately conservative when recency or the observed uncertainty
        # interval is weaker than the long-run mean.
        conservative_expectancy = None
        if sample:
            estimates = [expectancy or 0.0, recent_expectancy if recent_expectancy is not None else expectancy or 0.0]
            if ci_low is not None:
                estimates.append(ci_low)
            conservative_expectancy = min(estimates)
        evidence_weight = min(sample / 30.0, 1.0)
        evidence_component = max(-1.0, min(1.0, conservative_expectancy or 0.0)) * evidence_weight
        transition_weight = min(transition_sample / 20.0, 1.0)
        transition_component = max(-1.0, min(1.0, transition_expectancy or 0.0)) * transition_weight
        score = max(0.0, min(100.0, _finite(setup_quality) * 0.55 + fit * 25.0 + (evidence_component + 1.0) * 7.5 + (transition_component + 1.0) * 5.0))
        reasons = [f"fits {regime.lower()} regime", f"fits {timeframe} timeframe"]
        evidence_stage = "exploitation" if sample >= 20 and (expectancy or 0.0) > 0 else "exploration"
        if sample:
            uncertainty_text = f", conservative={conservative_expectancy:+.2f}R" if conservative_expectancy is not None else ""
            reasons.append(f"completed stable-regime evidence n={sample}, expectancy={expectancy:+.2f}R{uncertainty_text}")
        else:
            reasons.append("no completed stable-regime sample yet")
        if transition_sample:
            reasons.append(f"transition evidence n={transition_sample}, expectancy={transition_expectancy:+.2f}R")
        else:
            reasons.append("no completed transition-context sample yet")
        assessments.append(StrategyAssessment(
            identifier=definition.identifier, score=round(score, 4), confidence=evidence_class(sample, expectancy),
            sample_size=sample, expectancy_r=expectancy, applicable=True, rationale=tuple(reasons),
            evidence_stage=evidence_stage, transition_sample_size=transition_sample,
            transition_expectancy_r=transition_expectancy,
            conservative_expectancy_r=conservative_expectancy,
        ))
    assessments.sort(key=lambda item: (-item.score, item.identifier))
    return assessments


__all__ = ["StrategyAssessment", "evaluate_strategies", "evidence_class"]
