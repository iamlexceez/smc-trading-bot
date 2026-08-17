"""Evidence-based instrument specialization scoring.

Specialization is a long-horizon attention decision. It is deliberately separate
from the current opportunity score and never authorizes an order.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any, Mapping


SPECIALIZATION_WEIGHTS = {
    "statistical_performance": 0.25,
    "out_of_sample_performance": 0.20,
    "forward_demo_performance": 0.15,
    "stability": 0.10,
    "regime_coverage": 0.05,
    "execution_quality": 0.10,
    "account_size_suitability": 0.10,
    "portfolio_contribution": 0.05,
}

SAMPLE_TIERS = (
    (0, "UNVALIDATED"),
    (1, "VERY_WEAK"),
    (20, "PRELIMINARY"),
    (50, "DEVELOPING"),
    (100, "ESTABLISHED"),
    (200, "STRONG"),
)


@dataclass(frozen=True)
class InstrumentSpecializationScore:
    instrument: str
    raw_score: float
    evidence_factor: float
    adjusted_score: float
    sample_size: int
    evidence_tier: str
    component_scores: dict[str, float]
    missing_components: tuple[str, ...]
    reasons: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _clamp(value: Any, low: float = 0.0, high: float = 100.0) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return low
    if number != number:
        return low
    return max(low, min(high, number))


def evidence_tier(sample_size: int) -> str:
    n = max(0, int(sample_size or 0))
    tier = SAMPLE_TIERS[0][1]
    for lower, name in SAMPLE_TIERS:
        if n >= lower:
            tier = name
    return tier


def _sample_factor(sample_size: int, *, saturation: int = 200) -> float:
    if saturation <= 0:
        return 0.0
    return _clamp(sample_size, 0.0, float(saturation)) / float(saturation)


def expectancy_r(win_rate: float, average_win_r: float, average_loss_r: float) -> float:
    """Calculate expectancy from R outcomes; average loss is treated as magnitude."""
    win = _clamp(win_rate, 0.0, 1.0)
    loss = 1.0 - win
    return win * float(average_win_r) - loss * abs(float(average_loss_r))


def score_specialization(
    instrument: str,
    *,
    components: Mapping[str, Any],
    sample_size: int,
    out_of_sample_sample: int = 0,
    forward_sample: int = 0,
    recency_factor: float = 1.0,
    data_quality_factor: float = 1.0,
    weights: Mapping[str, float] | None = None,
    sample_saturation: int = 200,
) -> InstrumentSpecializationScore:
    """Return a transparent score without treating missing evidence as negative evidence.

    Missing component scores contribute zero to the raw score and are explicitly
    listed. The evidence multiplier prevents small samples from looking mastered.
    Callers may supply alternate weights as research policy variables.
    """
    active_weights = dict(weights or SPECIALIZATION_WEIGHTS)
    component_scores: dict[str, float] = {}
    missing: list[str] = []
    weighted_total = 0.0
    weight_total = 0.0
    for name, weight in active_weights.items():
        if name not in components or components.get(name) is None:
            missing.append(name)
            continue
        component_scores[name] = _clamp(components.get(name))
        positive_weight = max(0.0, float(weight))
        weighted_total += component_scores[name] * positive_weight
        weight_total += positive_weight
    raw = weighted_total / weight_total if weight_total else 0.0
    sample_factor = _sample_factor(sample_size, saturation=sample_saturation)
    oos_factor = _sample_factor(out_of_sample_sample, saturation=max(1, sample_saturation // 2))
    forward_factor = _sample_factor(forward_sample, saturation=max(1, sample_saturation // 2))
    evidence_factor = _clamp(
        (0.45 * sample_factor + 0.25 * oos_factor + 0.25 * forward_factor + 0.05 * _clamp(recency_factor, 0.0, 1.0))
        * _clamp(data_quality_factor, 0.0, 1.0),
        0.0,
        1.0,
    )
    adjusted = raw * evidence_factor
    reasons = [f"raw specialization score {raw:.2f}/100", f"evidence factor {evidence_factor:.3f}"]
    if missing:
        reasons.append("missing components remain unknown: " + ", ".join(missing))
    if sample_size == 0:
        reasons.append("no completed outcomes; instrument is unvalidated")
    return InstrumentSpecializationScore(
        instrument=str(instrument), raw_score=round(raw, 6),
        evidence_factor=round(evidence_factor, 6), adjusted_score=round(adjusted, 6),
        sample_size=max(0, int(sample_size or 0)), evidence_tier=evidence_tier(sample_size),
        component_scores=component_scores, missing_components=tuple(missing), reasons=tuple(reasons),
    )


__all__ = [
    "SPECIALIZATION_WEIGHTS", "SAMPLE_TIERS", "InstrumentSpecializationScore",
    "evidence_tier", "expectancy_r", "score_specialization",
]
