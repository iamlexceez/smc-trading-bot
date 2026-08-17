"""Canonical TradeSetup structured object for Setup Intelligence Engine V2."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping


@dataclass
class TradeSetup:
    setup_id: str
    timestamp: str
    instrument: str
    instrument_family: str
    market_regime: str
    direction: str
    analysis_timeframe: str
    context_timeframes: list[str] = field(default_factory=list)
    htf_bias: str = "UNKNOWN"
    intermediate_bias: str = "UNKNOWN"
    entry_timeframe_bias: str = "UNKNOWN"
    liquidity_target: float | None = None
    liquidity_sweep: dict[str, Any] = field(default_factory=dict)
    structure_event: dict[str, Any] = field(default_factory=dict)
    poi_type: str = ""
    poi_price: float | None = None
    poi_high: float | None = None
    poi_low: float | None = None
    trigger_type: str = ""
    trigger_price: float | None = None
    entry_price: float | None = None
    invalidation_price: float | None = None
    stop_loss: float | None = None
    target_type: str = ""
    target_price: float | None = None
    risk_distance: float = 0.0
    reward_distance: float = 0.0
    actual_rr: float = 0.0
    setup_model: str = ""
    setup_variant: str = ""
    context_evidence: dict[str, Any] = field(default_factory=dict)
    liquidity_evidence: dict[str, Any] = field(default_factory=dict)
    structure_evidence: dict[str, Any] = field(default_factory=dict)
    location_evidence: dict[str, Any] = field(default_factory=dict)
    trigger_evidence: dict[str, Any] = field(default_factory=dict)
    target_evidence: dict[str, Any] = field(default_factory=dict)
    contradictions: list[str] = field(default_factory=list)
    missing_evidence: list[str] = field(default_factory=list)
    quality_state: str = "PENDING"
    quality_score: float = 0.0
    expected_value: float = 0.0
    evidence_state: str = "INSUFFICIENT"
    execution_state: str = "PENDING"
    research_state: str = "PENDING"
    objective_state: str = "PENDING"
    portfolio_state: str = "PENDING"
    final_eligibility: bool = False
    decision_reason: str = ""
    created_at: str = ""
    updated_at: str = ""
    engine_version: str = "v2.0"

    def to_dict(self) -> dict[str, Any]:
        return {
            "setup_id": self.setup_id,
            "timestamp": self.timestamp,
            "instrument": self.instrument,
            "instrument_family": self.instrument_family,
            "market_regime": self.market_regime,
            "direction": self.direction,
            "analysis_timeframe": self.analysis_timeframe,
            "context_timeframes": list(self.context_timeframes),
            "htf_bias": self.htf_bias,
            "intermediate_bias": self.intermediate_bias,
            "entry_timeframe_bias": self.entry_timeframe_bias,
            "liquidity_target": self.liquidity_target,
            "liquidity_sweep": dict(self.liquidity_sweep),
            "structure_event": dict(self.structure_event),
            "poi_type": self.poi_type,
            "poi_price": self.poi_price,
            "poi_high": self.poi_high,
            "poi_low": self.poi_low,
            "trigger_type": self.trigger_type,
            "trigger_price": self.trigger_price,
            "entry_price": self.entry_price,
            "invalidation_price": self.invalidation_price,
            "stop_loss": self.stop_loss,
            "target_type": self.target_type,
            "target_price": self.target_price,
            "risk_distance": self.risk_distance,
            "reward_distance": self.reward_distance,
            "actual_rr": self.actual_rr,
            "setup_model": self.setup_model,
            "setup_variant": self.setup_variant,
            "context_evidence": dict(self.context_evidence),
            "liquidity_evidence": dict(self.liquidity_evidence),
            "structure_evidence": dict(self.structure_evidence),
            "location_evidence": dict(self.location_evidence),
            "trigger_evidence": dict(self.trigger_evidence),
            "target_evidence": dict(self.target_evidence),
            "contradictions": list(self.contradictions),
            "missing_evidence": list(self.missing_evidence),
            "quality_state": self.quality_state,
            "quality_score": self.quality_score,
            "expected_value": self.expected_value,
            "evidence_state": self.evidence_state,
            "execution_state": self.execution_state,
            "research_state": self.research_state,
            "objective_state": self.objective_state,
            "portfolio_state": self.portfolio_state,
            "final_eligibility": self.final_eligibility,
            "decision_reason": self.decision_reason,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "engine_version": self.engine_version,
        }
