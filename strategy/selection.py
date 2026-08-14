"""Evidence-aware selection among registry-defined strategy families.

This evaluator only ranks strategy applicability for an already observed setup.
It never creates an entry, bypasses broker checks, or promotes weak samples to
validated knowledge.
"""
from __future__ import annotations

from dataclasses import dataclass
import math
from typing import Any, Iterable

from strategy.registry import StrategyDefinition, applicable_strategies


@dataclass(frozen=True)
class StrategyAssessment:
    identifier: str
    score: float
    confidence: str
    sample_size: int
    expectancy_r: float | None
    applicable: bool
    rationale: tuple[str, ...]


def _finite(value: Any, default: float = 0.0) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    return number if math.isfinite(number) else default


def evidence_class(sample_size: int, expectancy_r: float | None) -> str:
    """Documented confidence labels; sample count prevents overclaiming.

    UNKNOWN=<3 completed outcomes; EARLY=3–9; OBSERVED=10–19;
    PROMISING=20–49; VALIDATED=50–99; STRONG_EVIDENCE=100+. Confidence
    measures sample depth only; displayed expectancy remains the separate,
    potentially negative outcome estimate.
    """
    del expectancy_r
    if sample_size < 3:
        return "UNKNOWN"
    if sample_size < 10:
        return "EARLY"
    if sample_size < 20:
        return "OBSERVED"
    if sample_size < 50:
        return "PROMISING"
    if sample_size < 100:
        return "VALIDATED"
    return "STRONG_EVIDENCE"


def evaluate_strategies(
    *, regime: str, timeframe: str, observed_features: Iterable[str], setup_quality: float,
    evidence_by_strategy: dict[str, dict[str, Any]],
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
        sample = max(0, int(_finite(metric.get("sample_size"))))
        expectancy_raw = metric.get("expectancy_r")
        expectancy = _finite(expectancy_raw, 0.0) if sample else None
        # Applicable registry definition is already regime/timeframe/feature-fit.
        fit = 1.0
        evidence_weight = min(sample / 30.0, 1.0)
        evidence_component = max(-1.0, min(1.0, expectancy or 0.0)) * evidence_weight
        score = max(0.0, min(100.0, _finite(setup_quality) * 0.60 + fit * 25.0 + (evidence_component + 1.0) * 7.5))
        reasons = [f"fits {regime.lower()} regime", f"fits {timeframe} timeframe"]
        if sample:
            reasons.append(f"completed context evidence n={sample}, expectancy={expectancy:+.2f}R")
        else:
            reasons.append("no completed context sample yet")
        assessments.append(StrategyAssessment(
            identifier=definition.identifier, score=round(score, 4), confidence=evidence_class(sample, expectancy),
            sample_size=sample, expectancy_r=expectancy, applicable=True, rationale=tuple(reasons),
        ))
    assessments.sort(key=lambda item: (-item.score, item.identifier))
    return assessments


__all__ = ["StrategyAssessment", "evaluate_strategies", "evidence_class"]
