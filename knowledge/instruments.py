"""Evidence-governed instrument roles for research cohort management."""
from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any


ROLES = ("CORE", "CHALLENGER", "RESEARCH", "QUARANTINED")


@dataclass(frozen=True)
class InstrumentClassification:
    instrument: str
    role: str
    broker_eligible: bool
    sample_size: int
    expectancy_r: float | None
    max_drawdown_r: float | None
    execution_reliability: float | None
    reason: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _number(value: Any) -> float | None:
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def classify_instrument(
    instrument: str,
    *,
    broker_eligible: bool,
    evidence: dict[str, Any] | None,
    minimum_sample_size: int,
    max_manageable_drawdown_r: float,
    minimum_execution_reliability: float,
) -> InstrumentClassification:
    """Classify without inventing evidence and without authorizing an order."""
    metric = dict(evidence or {})
    sample = max(0, int(metric.get("sample_size") or 0))
    expectancy = _number(metric.get("expectancy_r"))
    drawdown = _number(metric.get("max_drawdown_r"))
    reliability = _number(metric.get("execution_reliability"))
    name = str(instrument or "").strip()
    if not broker_eligible:
        return InstrumentClassification(name, "QUARANTINED", False, sample, expectancy, drawdown, reliability, "Broker eligibility is not currently verified.")
    if sample == 0 or expectancy is None:
        return InstrumentClassification(name, "RESEARCH", True, sample, expectancy, drawdown, reliability, "No completed evidence is available; instrument remains in research.")
    if expectancy <= 0:
        return InstrumentClassification(name, "QUARANTINED", True, sample, expectancy, drawdown, reliability, "Negative expectancy is negative evidence, not an unknown sample.")
    if sample < max(1, int(minimum_sample_size)):
        return InstrumentClassification(name, "RESEARCH", True, sample, expectancy, drawdown, reliability, "Positive evidence exists but the configured sample depth is not complete.")
    if drawdown is None or reliability is None:
        return InstrumentClassification(name, "CHALLENGER", True, sample, expectancy, drawdown, reliability, "Positive evidence exists, but drawdown or execution reliability is unknown.")
    if drawdown <= float(max_manageable_drawdown_r) and reliability >= float(minimum_execution_reliability):
        return InstrumentClassification(name, "CORE", True, sample, expectancy, drawdown, reliability, "Positive out-of-sample/forward evidence is stable and execution is reliable.")
    return InstrumentClassification(name, "CHALLENGER", True, sample, expectancy, drawdown, reliability, "Positive evidence exists but stability or execution criteria are not yet core-grade.")


__all__ = ["ROLES", "InstrumentClassification", "classify_instrument"]
