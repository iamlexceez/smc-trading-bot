"""Portfolio contribution and diversification governance for Core selection."""
from __future__ import annotations

from dataclasses import asdict, dataclass
from math import sqrt
from typing import Any, Iterable


@dataclass(frozen=True)
class PortfolioSelection:
    selected: tuple[str, ...]
    rejected: tuple[dict[str, Any], ...]
    correlation_threshold: float

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def pearson_correlation(left: Iterable[float], right: Iterable[float]) -> float | None:
    a = [float(value) for value in left]
    b = [float(value) for value in right]
    n = min(len(a), len(b))
    if n < 2:
        return None
    a, b = a[-n:], b[-n:]
    mean_a = sum(a) / n
    mean_b = sum(b) / n
    cov = sum((x - mean_a) * (y - mean_b) for x, y in zip(a, b))
    var_a = sum((x - mean_a) ** 2 for x in a)
    var_b = sum((y - mean_b) ** 2 for y in b)
    if var_a <= 0 or var_b <= 0:
        return None
    return cov / sqrt(var_a * var_b)


def select_diversified_core(
    rankings: list[dict[str, Any]],
    *,
    return_series: dict[str, list[float]] | None,
    max_core_instruments: int,
    correlation_threshold: float,
) -> PortfolioSelection:
    """Select an earned Core set without filling slots or using broker order."""
    selected: list[str] = []
    rejected: list[dict[str, Any]] = []
    series = return_series or {}
    limit = max(0, min(10, int(max_core_instruments)))
    threshold = max(0.0, min(1.0, float(correlation_threshold)))
    for item in rankings:
        instrument = str(item.get("instrument") or "")
        if item.get("role") not in {"CORE", "CORE_STRONG"}:
            continue
        if len(selected) >= limit:
            rejected.append({"instrument": instrument, "reason": "Core ceiling reached; no empty slots are filled."})
            continue
        correlations = []
        for existing in selected:
            correlation = pearson_correlation(series.get(instrument, []), series.get(existing, []))
            if correlation is not None:
                correlations.append((existing, correlation))
        high = [(existing, correlation) for existing, correlation in correlations if abs(correlation) >= threshold]
        if high:
            stronger = max(high, key=lambda pair: pair[1])
            rejected.append({
                "instrument": instrument,
                "reason": f"Highly correlated with stronger selected Core instrument {stronger[0]} ({stronger[1]:+.3f}); retained as challenger/review candidate.",
                "correlations": dict(correlations),
            })
            continue
        selected.append(instrument)
    return PortfolioSelection(tuple(selected), tuple(rejected), threshold)


__all__ = ["PortfolioSelection", "pearson_correlation", "select_diversified_core"]
