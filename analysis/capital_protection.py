"""Broker-observed capital protection context for active-position management.

The score is an input to an experimental management policy.  It never opens a
trade, overrides broker validation, widens a stop, or terminalizes an account.
"""

from __future__ import annotations

from dataclasses import dataclass
from math import isfinite
from typing import Any, Iterable, Mapping


def _number(value: Any, fallback: float = 0.0) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return fallback
    return result if isfinite(result) else fallback


def _clamp(value: float, lower: float = 0.0, upper: float = 1.0) -> float:
    return max(lower, min(upper, value))


@dataclass(frozen=True)
class CapitalProtectionContext:
    """Continuous fresh-broker risk context for existing position management."""

    score: float
    level: str
    equity_position: float
    drawdown: float
    margin_pressure: float
    unrealized_loss_pressure: float
    position_concentration: float
    open_position_count: int
    reason: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "score": self.score,
            "level": self.level,
            "equity_position": self.equity_position,
            "drawdown": self.drawdown,
            "margin_pressure": self.margin_pressure,
            "unrealized_loss_pressure": self.unrealized_loss_pressure,
            "position_concentration": self.position_concentration,
            "open_position_count": self.open_position_count,
            "reason": self.reason,
        }


def calculate_capital_protection(
    *,
    account: Mapping[str, Any],
    positions: Iterable[Mapping[str, Any]],
    phase: Mapping[str, Any] | None = None,
    management_evidence: Mapping[str, Any] | None = None,
) -> CapitalProtectionContext:
    """Calculate a continuous protection context from current broker evidence.

    The components remain visible in telemetry so the policy learner can assess
    management outcomes by protection regime.  No component by itself causes a
    forced close or a new-order decision.
    """
    rows = [dict(position or {}) for position in positions]
    equity = max(0.0, _number(account.get("equity")))
    balance = max(0.0, _number(account.get("balance")))
    free_margin = _number(account.get("free_margin"))
    margin = max(0.0, _number(account.get("margin")))

    phase = dict(phase or {})
    start = _number(phase.get("starting_equity", phase.get("planned_start_equity", balance)))
    target = _number(phase.get("target_equity"))
    if target > start > 0:
        equity_position = _clamp((equity - start) / (target - start))
    else:
        equity_position = 0.5

    reference_equity = max(start, balance, equity, 1.0)
    drawdown = _clamp((reference_equity - equity) / reference_equity)
    margin_utilization = _clamp(margin / max(equity, 1.0))
    margin_pressure = 1.0 if free_margin <= 0 else _clamp(margin_utilization)

    unrealized = sum(_number(row.get("profit")) + _number(row.get("swap")) + _number(row.get("commission")) for row in rows)
    unrealized_loss_pressure = _clamp(max(0.0, -unrealized) / max(equity, 1.0))
    volumes = [max(0.0, _number(row.get("volume"))) for row in rows]
    total_volume = sum(volumes)
    position_concentration = _clamp(max(volumes, default=0.0) / total_volume) if total_volume > 0 else 0.0

    evidence = dict(management_evidence or {})
    observed_drawdown = _clamp(max(0.0, _number(evidence.get("average_mae_r"))) / 5.0)
    # Continuous components; they describe vulnerability rather than prescribe
    # a fixed stop, target, or exposure rule.  The active experimental policy
    # decides whether and how to respond.
    score = _clamp(
        0.28 * (1.0 - equity_position)
        + 0.22 * drawdown
        + 0.30 * margin_pressure
        + 0.10 * unrealized_loss_pressure
        + 0.05 * position_concentration
        + 0.05 * observed_drawdown
    )
    if score >= 0.75:
        level = "HIGH"
    elif score >= 0.45:
        level = "ELEVATED"
    else:
        level = "NORMAL"
    reasons = []
    if margin_pressure >= 0.8:
        reasons.append("margin pressure")
    if drawdown >= 0.25:
        reasons.append("drawdown")
    if unrealized_loss_pressure >= 0.10:
        reasons.append("unrealized loss")
    if position_concentration >= 0.70 and len(rows) > 1:
        reasons.append("position concentration")
    if not reasons:
        reasons.append("phase/equity context")
    return CapitalProtectionContext(
        score=score,
        level=level,
        equity_position=equity_position,
        drawdown=drawdown,
        margin_pressure=margin_pressure,
        unrealized_loss_pressure=unrealized_loss_pressure,
        position_concentration=position_concentration,
        open_position_count=len(rows),
        reason=", ".join(reasons),
    )


__all__ = ["CapitalProtectionContext", "calculate_capital_protection"]
