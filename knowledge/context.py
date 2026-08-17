"""Normalized context contract used by knowledge and strategy routing."""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Iterable


REGIMES = (
    "TRENDING_BULLISH", "TRENDING_BEARISH", "RANGING", "EXPANSION",
    "CONTRACTION", "BREAKOUT", "POST_BREAKOUT", "REVERSAL", "TRANSITION",
    "CHAOTIC", "UNKNOWN",
)

# Backward-compatible labels emitted by older analysis code.
_REGIME_ALIASES = {
    "TRENDING": "TRENDING_BULLISH",
    "BULLISH": "TRENDING_BULLISH",
    "BEARISH": "TRENDING_BEARISH",
    "COMPRESSION": "CONTRACTION",
    "EXHAUSTION": "REVERSAL",
}


def normalize_regime(value: Any) -> str:
    label = str(value or "UNKNOWN").strip().upper().replace("-", "_").replace(" ", "_")
    label = _REGIME_ALIASES.get(label, label)
    return label if label in REGIMES else "UNKNOWN"


@dataclass(frozen=True)
class MarketContext:
    symbol: str
    timeframe: str
    regime: str = "UNKNOWN"
    htf_bias: str = "UNKNOWN"
    top_down_alignment: str = "UNKNOWN"
    trend_direction: str = "UNKNOWN"
    volatility_state: str = "UNKNOWN"
    momentum_state: str = "UNKNOWN"
    liquidity_state: str = "UNKNOWN"
    session: str = "UNKNOWN"
    quote_fresh: bool = False
    execution_quality: str = "UNKNOWN"
    observed_features: tuple[str, ...] = ()
    contradictions: tuple[str, ...] = ()
    source: str = "closed_candles_plus_broker_state"
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @property
    def normalized_regime(self) -> str:
        return normalize_regime(self.regime)


def build_context(
    symbol: str,
    timeframe: str,
    *,
    regime: Any = "UNKNOWN",
    htf_bias: Any = "UNKNOWN",
    top_down_alignment: Any = "UNKNOWN",
    observed_features: Iterable[str] = (),
    contradictions: Iterable[str] = (),
    **metadata: Any,
) -> MarketContext:
    return MarketContext(
        symbol=str(symbol),
        timeframe=str(timeframe),
        regime=normalize_regime(regime),
        htf_bias=str(htf_bias or "UNKNOWN").upper(),
        top_down_alignment=str(top_down_alignment or "UNKNOWN").upper(),
        observed_features=tuple(sorted({str(item) for item in observed_features})),
        contradictions=tuple(str(item) for item in contradictions),
        metadata=dict(metadata),
    )


__all__ = ["REGIMES", "MarketContext", "build_context", "normalize_regime"]
