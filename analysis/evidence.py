"""Descriptive statistics for completed DEMO or causal-replay outcomes.

This module never selects a trade or changes an active policy. It quantifies the
uncertainty and distribution of already-recorded observations for transparent
research reporting and bounded evidence-aware ranking.
"""
from __future__ import annotations

import math
from statistics import mean, stdev
from typing import Any, Iterable


def _finite(value: Any, default: float = 0.0) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    return number if math.isfinite(number) else default


def _quantile(values: list[float], probability: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    location = max(0.0, min(1.0, probability)) * (len(ordered) - 1)
    lower = int(math.floor(location))
    upper = int(math.ceil(location))
    if lower == upper:
        return ordered[lower]
    return ordered[lower] + (ordered[upper] - ordered[lower]) * (location - lower)


def completed_outcome_statistics(rows: Iterable[dict[str, Any]], *, recency_half_life: float = 20.0) -> dict[str, Any]:
    """Summarise finalized outcomes without claiming statistical certainty.

    Rows must be in chronological order. The returned normal-approximation
    interval is labelled unavailable until two observations exist and is only a
    descriptive uncertainty range, not a proof of profitability.
    """
    prepared: list[dict[str, float]] = []
    for row in rows:
        pnl = _finite(row.get("pnl_r"), float("nan"))
        if not math.isfinite(pnl):
            continue
        prepared.append({
            "pnl_r": pnl,
            "mae_r": _finite(row.get("mae_r")),
            "mfe_r": _finite(row.get("mfe_r")),
            "target_r": max(0.0, _finite(row.get("target_r"))),
        })
    sample_size = len(prepared)
    outcomes = [row["pnl_r"] for row in prepared]
    mae = [row["mae_r"] for row in prepared]
    mfe = [row["mfe_r"] for row in prepared]
    expectancy = mean(outcomes) if outcomes else None
    dispersion = stdev(outcomes) if sample_size >= 2 else None
    standard_error = (dispersion / math.sqrt(sample_size)) if dispersion is not None and sample_size else None
    ci_low = expectancy - 1.96 * standard_error if standard_error is not None and expectancy is not None else None
    ci_high = expectancy + 1.96 * standard_error if standard_error is not None and expectancy is not None else None

    if outcomes:
        half_life = max(1.0, float(recency_half_life))
        weights = [0.5 ** ((sample_size - 1 - index) / half_life) for index in range(sample_size)]
        recent_expectancy = sum(value * weight for value, weight in zip(outcomes, weights)) / sum(weights)
    else:
        recent_expectancy = None
    target_candidates = [row for row in prepared if row["target_r"] > 0]
    target_reach = (
        sum(row["mfe_r"] >= row["target_r"] for row in target_candidates) / len(target_candidates)
        if target_candidates else None
    )
    return {
        "sample_size": sample_size,
        "expectancy_r": expectancy,
        "recent_expectancy_r": recent_expectancy,
        "pnl_stddev_r": dispersion,
        "expectancy_standard_error_r": standard_error,
        "expectancy_ci95_low_r": ci_low,
        "expectancy_ci95_high_r": ci_high,
        "mae_p50_r": _quantile(mae, 0.50),
        "mae_p90_r": _quantile(mae, 0.90),
        "mfe_p50_r": _quantile(mfe, 0.50),
        "mfe_p90_r": _quantile(mfe, 0.90),
        "target_reach_probability": target_reach,
        "basis": "completed_outcomes_only",
    }


__all__ = ["completed_outcome_statistics"]
