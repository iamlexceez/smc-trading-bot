"""Account-scale economic regimes used for research and reporting context."""
from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any


@dataclass(frozen=True)
class EconomicRegime:
    name: str
    lower_bound: float
    upper_bound: float | None
    objective: str
    note: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


ECONOMIC_REGIMES = (
    EconomicRegime("SURVIVAL", 10.0, 25.0, "Preserve viable experimentation under small-account constraints.", "Economic context only; not an automatic trade rule."),
    EconomicRegime("CONTROLLED_GROWTH", 25.0, 50.0, "Measure repeatable growth while retaining research breadth.", "Economic context only; not an automatic trade rule."),
    EconomicRegime("GROWTH", 50.0, 100.0, "Evaluate scalable policy behaviour and execution quality.", "Economic context only; not an automatic trade rule."),
    EconomicRegime("EXPANSION", 100.0, 200.0, "Evaluate capacity, diversification, and geometric growth.", "Economic context only; not an automatic trade rule."),
)


def classify_economic_regime(effective_capital: float, *, regimes=ECONOMIC_REGIMES) -> EconomicRegime | None:
    try:
        capital = float(effective_capital)
    except (TypeError, ValueError):
        return None
    for regime in regimes:
        if capital >= regime.lower_bound and (regime.upper_bound is None or capital < regime.upper_bound):
            return regime
    return None


def geometric_growth(returns_r: list[float] | tuple[float, ...]) -> float:
    """Return product(1+R_i); invalid factors are rejected rather than coerced."""
    product = 1.0
    for raw in returns_r:
        factor = 1.0 + float(raw)
        if factor <= 0:
            raise ValueError("Geometric growth is undefined for a return factor at or below zero")
        product *= factor
    return product


__all__ = ["EconomicRegime", "ECONOMIC_REGIMES", "classify_economic_regime", "geometric_growth"]
