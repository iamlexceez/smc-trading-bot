"""Evidence-driven Instrument DNA system."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Optional


@dataclass(frozen=True)
class DNAProperty:
    value: Any
    source: str
    sample_size: int
    period_days: int
    confidence: float
    updated_at: str


@dataclass(frozen=True)
class InstrumentDNA:
    instrument: str
    best_regimes: list[str] = field(default_factory=list)
    weak_regimes: list[str] = field(default_factory=list)
    best_timeframes: list[str] = field(default_factory=list)
    best_strategies: list[str] = field(default_factory=list)
    best_concepts: list[str] = field(default_factory=list)
    weak_concepts: list[str] = field(default_factory=list)
    execution_quality: float = 0.0
    small_account_suitability: float = 0.0
    average_stop_distance: float = 0.0
    typical_excursion: float = 0.0
    historical_expectancy: float = 0.0
    oos_expectancy: float = 0.0
    forward_expectancy: float = 0.0
    drawdown: float = 0.0
    stability: float = 0.0
    correlation: float = 0.0
    current_edge: str = "UNKNOWN"
    metadata: dict[str, DNAProperty] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        import dataclasses
        return dataclasses.asdict(self)


class InstrumentDNAEngine:
    """Manages and updates evidence-driven Instrument DNA profiles."""
    
    @staticmethod
    def build_dna(instrument: str, evidence_rows: list[dict[str, Any]]) -> InstrumentDNA:
        """Derive DNA from realized evidence rows."""
        if not evidence_rows:
            return InstrumentDNA(instrument=instrument)
            
        # Group outcomes by regime, strategy, concept
        # Calculate expectancy, stability, etc.
        # (This is a simplified version; real implementation would use statistical aggregators)
        
        return InstrumentDNA(
            instrument=instrument,
            current_edge="RESEARCH",
            forward_expectancy=sum(float(r.get("pnl_r", 0.0)) for r in evidence_rows) / len(evidence_rows),
            stability=0.5, # Placeholder
            drawdown=0.0, # Placeholder
        )
