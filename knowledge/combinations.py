"""Evidence utilities for independent concept and combination comparisons."""
from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any


@dataclass(frozen=True)
class CombinationResult:
    combination_id: str
    baseline_expectancy_r: float | None
    combined_expectancy_r: float | None
    incremental_expectancy_r: float | None
    sample_size: int
    state: str
    reason: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _value(metric: dict[str, Any] | None) -> float | None:
    if not metric or metric.get("expectancy_r") is None:
        return None
    try:
        return float(metric["expectancy_r"])
    except (TypeError, ValueError):
        return None


def evaluate_combination(
    combination_id: str,
    *,
    single_a: dict[str, Any] | None,
    single_b: dict[str, Any] | None,
    combined: dict[str, Any] | None,
    minimum_sample_size: int,
    minimum_incremental_expectancy_r: float = 0.0,
) -> CombinationResult:
    """Compare A, B, and A+B without treating missing data as negative evidence."""
    a = _value(single_a)
    b = _value(single_b)
    both = _value(combined)
    sample = max(0, int((combined or {}).get("sample_size") or 0))
    if both is None or a is None or b is None:
        known_components = [value for value in (a, b) if value is not None]
        baseline = max(known_components) if known_components else None
        return CombinationResult(
            combination_id=str(combination_id), baseline_expectancy_r=baseline,
            combined_expectancy_r=both, incremental_expectancy_r=None, sample_size=sample,
            state="INSUFFICIENT_EVIDENCE", reason="One or more component/combination expectancy estimates are unavailable.",
        )
    baseline = max(a, b)
    incremental = both - baseline
    if sample < max(1, int(minimum_sample_size)):
        state = "INSUFFICIENT_EVIDENCE"
        reason = f"Combination sample {sample} is below the configured evidence depth."
    elif incremental < 0:
        state = "CONFLICTING"
        reason = f"Combined expectancy is {incremental:+.3f}R below the stronger component baseline."
    elif incremental < float(minimum_incremental_expectancy_r):
        state = "REDUNDANT"
        reason = f"Incremental expectancy {incremental:+.3f}R does not meet the configured improvement threshold."
    else:
        state = "PROMOTABLE_CANDIDATE"
        reason = f"Combined expectancy adds {incremental:+.3f}R over the stronger single-concept baseline."
    return CombinationResult(
        combination_id=str(combination_id), baseline_expectancy_r=baseline,
        combined_expectancy_r=both, incremental_expectancy_r=incremental,
        sample_size=sample, state=state, reason=reason,
    )


__all__ = ["CombinationResult", "evaluate_combination"]
