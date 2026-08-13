"""SMC signal transport and reporting.

Hard setup validity is owned by ``strategy.setup_validator``. Quality ranking is
owned by ``strategy.setup_scorer``. This module deliberately performs neither
external-market sentiment analysis nor heuristic score generation.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional


@dataclass
class ScoreFactor:
    """Compatibility view for a transparent score component."""

    name: str
    score: float
    weight: float
    max_points: float = 0.0
    detail: str = ""


@dataclass
class TradeSignal:
    symbol: str
    direction: str
    entry_price: float
    stop_loss: float
    take_profit: float
    score: float
    rr_ratio: float
    suggested_risk: float = 0.75
    factors: list[ScoreFactor] = field(default_factory=list)
    structure: Any = None
    zones: list[Any] = field(default_factory=list)
    timeframe: str = "M15"
    entry_mode: str = "confirmed"
    setup_type: str = "Liquidity Sweep Reversal"
    created_at: Optional[str] = None
    expires_at: Optional[str] = None
    validation: Any = None
    quality_factors: list[Any] = field(default_factory=list)
    target_source: str = ""
    setup_id: Optional[int] = None
    policy_version: str = ""
    experiment_id: Optional[int] = None
    experimental_policy: dict[str, Any] = field(default_factory=dict)
    passed: bool = False
    rejection_reason: str = ""

    @property
    def passed_gates(self) -> bool:
        return self.passed


def format_signal_report(signal: TradeSignal) -> str:
    """Render validity gates separately from the non-bypassable quality rank."""
    header = [
        f"📊 **{signal.symbol}** — `{signal.direction}` ({signal.timeframe})",
        f"Setup: `{signal.setup_type}` | Entry model: `{signal.entry_mode}`",
        f"Quality rank: `{signal.score:.1f}/100` | Market-derived RR: `1:{signal.rr_ratio:.2f}`",
        "",
        f"Entry: `{signal.entry_price:.5f}` | SL: `{signal.stop_loss:.5f}` | TP: `{signal.take_profit:.5f}`",
    ]
    validation = signal.validation
    if validation is not None:
        header.extend(["", "**Hard validity gates**"])
        for check in getattr(validation, "checks", []):
            state = "✅" if check.passed else "❌"
            header.append(f"{state} {check.name}: {check.detail}")

    factors = signal.quality_factors or signal.factors
    if factors:
        header.extend(["", "**Quality ranking — cannot override hard gates**"])
        for factor in factors:
            if hasattr(factor, "points"):
                header.append(f"• {factor.name}: `{factor.points:.1f}/{factor.maximum:.1f}` — {factor.detail}")
            else:
                header.append(f"• {factor.name}: `{factor.score:.1f}` — {factor.detail}")

    header.extend(["", ("✅ **VALIDATED — execution still subject to portfolio and broker checks**" if signal.passed else f"❌ **NOT EXECUTABLE**: {signal.rejection_reason or 'Hard validity or safety check failed'}")])
    return "\n".join(header)


__all__ = ["ScoreFactor", "TradeSignal", "format_signal_report"]
