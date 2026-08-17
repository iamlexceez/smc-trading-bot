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
    retired_veto: Optional[str] = None
    passed: bool = False
    rejection_reason: str = ""
    regime: str = "UNKNOWN"
    previous_regime: str = "UNKNOWN"
    regime_transition: str = "UNKNOWN"
    primary_thesis: dict[str, Any] = field(default_factory=dict)
    alternative_theses: list[dict[str, Any]] = field(default_factory=list)
    evidence_summary: dict[str, Any] = field(default_factory=dict)
    expected_value_r: Optional[float] = None
    invalidation_reason: str = ""
    management_plan: dict[str, Any] = field(default_factory=dict)
    causality: dict[str, Any] = field(default_factory=dict)
    # Research acceptance and objective-trading approval are deliberately separate.
    research_decision: str = "RESEARCH_REJECTED"
    trading_decision: str = "DEFERRED"
    final_state: str = "PENDING_FINAL_VALIDATION"
    execution_class: str = "RESEARCH_ONLY"
    evidence_classification: str = "INSUFFICIENT"
    confidence_classification: str = "UNVALIDATED"
    research_reason: str = ""
    trading_reason: str = ""
    learning_objective: str = ""
    target_alternatives: list[dict[str, Any]] = field(default_factory=list)
    htf_context: list[dict[str, Any]] = field(default_factory=list)
    htf_bias_status: str = "UNKNOWN"
    setup_quality_components: dict[str, Any] = field(default_factory=dict)
    execution_class_reason: str = ""
    htf_relationship: str = "UNKNOWN"

    @property
    def passed_gates(self) -> bool:
        return self.passed


def format_signal_report(signal: TradeSignal) -> str:
    """Render validity gates separately from the non-bypassable quality rank."""
    htf_text = ", ".join(
        f"{item.get('timeframe', 'TF')}={item.get('bias', 'UNKNOWN')}"
        for item in signal.htf_context
    ) or "unavailable"
    header = [
        f"📊 **{signal.symbol}** — `{signal.direction}` ({signal.timeframe})",
        f"Setup: `{signal.setup_type}` | Entry model: `{signal.entry_mode}`",
        f"Quality rank: `{signal.score:.1f}/100` | Market-derived RR: `1:{signal.rr_ratio:.2f}`",
        f"Classification: `{signal.execution_class}` | Research: `{signal.research_decision}` | Objective trading: `{signal.trading_decision}` | Final state: `{signal.final_state}`",
        f"Class reason: {signal.execution_class_reason or 'Pending final broker and portfolio validation.'}",
        f"Evidence: `{signal.evidence_classification}` | Confidence: `{signal.confidence_classification}`",
        f"Top-down context: `{signal.htf_bias_status}` | {htf_text}",
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

    if signal.trading_reason:
        header.extend([f"Trading-gate reason: {signal.trading_reason}"])
    header.extend(["", ("✅ **RESEARCH ACCEPTED — objective trading remains independently gated**" if signal.research_decision == "RESEARCH_ACCEPTED" else f"❌ **RESEARCH REJECTED**: {signal.rejection_reason or 'Research gate failed'}")])
    return "\n".join(header)


__all__ = ["ScoreFactor", "TradeSignal", "format_signal_report"]
