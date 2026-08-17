"""Evidence-governed instrument roles for research cohort management."""
from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any


ROLES = ("CORE", "CANDIDATE", "EXPLORATORY", "UNDER_REVIEW", "QUARANTINED")


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
    
    # Empirical Learning Lifecycle
    if sample < 10:
        return InstrumentClassification(name, "EXPLORATORY", True, sample, expectancy, drawdown, reliability, "Insufficient sample (<10) for evaluation.")
    
    if expectancy is not None and expectancy <= 0:
        return InstrumentClassification(name, "UNDER_REVIEW", True, sample, expectancy, drawdown, reliability, "Negative expectancy observed; demoted to under-review.")
    
    if drawdown is not None and drawdown > float(max_manageable_drawdown_r):
        return InstrumentClassification(name, "UNDER_REVIEW", True, sample, expectancy, drawdown, reliability, f"Drawdown {drawdown:.2f}R exceeds limit {max_manageable_drawdown_r:.2f}R.")

    if sample < 50:
        return InstrumentClassification(name, "CANDIDATE", True, sample, expectancy, drawdown, reliability, "Positive evidence exists but sample size (<50) is developing.")
    
    if reliability is not None and reliability < float(minimum_execution_reliability):
        return InstrumentClassification(name, "CANDIDATE", True, sample, expectancy, drawdown, reliability, "Execution reliability below core standard.")

    if expectancy is not None and expectancy > 0 and sample >= 50:
        return InstrumentClassification(name, "CORE", True, sample, expectancy, drawdown, reliability, "Strong empirical evidence and stable execution.")
        
    return InstrumentClassification(name, "CANDIDATE", True, sample, expectancy, drawdown, reliability, "Developing evidence profile.")


__all__ = ["ROLES", "InstrumentClassification", "classify_instrument"]
