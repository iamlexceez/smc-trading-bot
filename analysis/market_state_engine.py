"""Unified Market State Engine for Trading Intelligence V2."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Optional


@dataclass(frozen=True)
class MarketState:
    instrument: str
    timestamp: str
    price: float
    spread: float
    quote_age: float
    volatility: float
    trend: str
    range_state: str
    regime: str
    session: str
    liquidity_state: dict[str, Any]
    structure_state: dict[str, Any]
    htf_bias: str
    intermediate_bias: str
    entry_timeframe_state: str
    recent_events: list[str] = field(default_factory=list)
    active_zones: list[dict[str, Any]] = field(default_factory=list)
    market_condition: str = "NORMAL"
    execution_condition: str = "GOOD"

    def to_dict(self) -> dict[str, Any]:
        import dataclasses
        return dataclasses.asdict(self)


class MarketStateEngine:
    """Builds a unified, timestamped representation of the market state."""
    
    @staticmethod
    def build_state(
        instrument: str,
        price_data: dict[str, Any],
        analysis_data: dict[str, Any],
        context_data: dict[str, Any]
    ) -> MarketState:
        now = datetime.now(timezone.utc).isoformat()
        
        return MarketState(
            instrument=instrument,
            timestamp=now,
            price=float(price_data.get("price", 0.0)),
            spread=float(price_data.get("spread", 0.0)),
            quote_age=float(price_data.get("quote_age", 0.0)),
            volatility=float(analysis_data.get("volatility", 0.0)),
            trend=str(analysis_data.get("trend", "UNKNOWN")),
            range_state=str(analysis_data.get("range_state", "UNKNOWN")),
            regime=str(context_data.get("regime", "UNKNOWN")),
            session=str(context_data.get("session", "UNKNOWN")),
            liquidity_state=dict(analysis_data.get("liquidity", {})),
            structure_state=dict(analysis_data.get("structure", {})),
            htf_bias=str(analysis_data.get("htf_bias", "UNKNOWN")),
            intermediate_bias=str(analysis_data.get("intermediate_bias", "UNKNOWN")),
            entry_timeframe_state=str(analysis_data.get("entry_tf_state", "UNKNOWN")),
            recent_events=list(analysis_data.get("events", [])),
            active_zones=list(analysis_data.get("zones", [])),
            market_condition=str(context_data.get("market_condition", "NORMAL")),
            execution_condition=str(context_data.get("execution_condition", "GOOD")),
        )
