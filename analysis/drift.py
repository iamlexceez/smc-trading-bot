"""Model drift classification for DEMO champion/challenger governance."""
from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any


@dataclass(frozen=True)
class DriftResult:
    state: str
    baseline_expectancy_r: float | None
    current_expectancy_r: float | None
    expectancy_change_r: float | None
    baseline_sample_size: int
    current_sample_size: int
    reason: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _number(metric: dict[str, Any] | None, key: str) -> float | None:
    try:
        value = (metric or {}).get(key)
        return None if value is None else float(value)
    except (TypeError, ValueError):
        return None


def detect_model_drift(
    baseline: dict[str, Any] | None,
    current: dict[str, Any] | None,
    *,
    minimum_sample_size: int,
    max_expectancy_decline_r: float,
) -> DriftResult:
    """Classify drift without turning sparse data into a negative conclusion."""
    base_n = max(0, int((baseline or {}).get("sample_size") or 0))
    current_n = max(0, int((current or {}).get("sample_size") or 0))
    base_e = _number(baseline, "expectancy_r")
    current_e = _number(current, "expectancy_r")
    change = None if base_e is None or current_e is None else current_e - base_e
    if base_e is None or current_e is None or current_n < max(1, int(minimum_sample_size)):
        return DriftResult(
            "INSUFFICIENT_EVIDENCE", base_e, current_e, change, base_n, current_n,
            "Current or baseline expectancy evidence is incomplete for drift classification.",
        )
    if change < -abs(float(max_expectancy_decline_r)):
        return DriftResult(
            "DRIFT", base_e, current_e, change, base_n, current_n,
            f"Current expectancy declined by {change:+.3f}R beyond the configured drift tolerance.",
        )
    return DriftResult(
        "STABLE", base_e, current_e, change, base_n, current_n,
        "Current expectancy remains within the configured drift tolerance.",
    )


__all__ = ["DriftResult", "detect_model_drift"]
