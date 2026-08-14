"""Adaptive TP/SL learning evidence built on the bot's existing policy and replay engines.

This module intentionally does *not* submit MT5 orders, modify live positions,
or maintain a competing champion database.  It turns broker-confirmed outcomes
and the existing candle-by-candle ``BacktestEngine`` replay into auditable
management observations.  Champion/challenger promotion remains owned by the
repository's immutable model governance in ``analysis.optimizer``.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime
from statistics import mean
from typing import Any, Iterable, Optional

from analysis.policies import PolicyEvaluator
from analysis.evidence import completed_outcome_statistics


@dataclass(frozen=True)
class ManagementObservation:
    """One completed TP/SL-management observation from real or replayed data."""

    ticket: Optional[int]
    symbol: str
    direction: str
    setup_type: str
    timeframe: str
    entry_price: float
    initial_stop: float
    initial_target: float
    initial_rr: float
    mae_r: float
    mfe_r: float
    final_r: float
    duration_seconds: int
    sl_modifications: int
    tp_modifications: int
    breakeven_activated: bool
    trailing_activated: bool
    partial_exit_used: bool
    exit_reason: str
    policy_version: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _duration_seconds(opened_at: Any, closed_at: Any) -> int:
    if not opened_at or not closed_at:
        return 0
    try:
        start = datetime.fromisoformat(str(opened_at).replace("Z", "+00:00"))
        end = datetime.fromisoformat(str(closed_at).replace("Z", "+00:00"))
        return max(0, int((end - start).total_seconds()))
    except (TypeError, ValueError):
        return 0


def observation_from_broker_trade(trade: dict[str, Any], actions: Iterable[dict[str, Any]]) -> ManagementObservation:
    """Build a completed observation from stored, broker-confirmed data only."""
    logs = list(actions)
    action_names = [str(row.get("action") or "") for row in logs]
    details = " ".join(str(row.get("details") or "").lower() for row in logs)
    return ManagementObservation(
        ticket=int(trade["ticket"]) if trade.get("ticket") is not None else None,
        symbol=str(trade.get("symbol") or ""),
        direction=str(trade.get("direction") or ""),
        setup_type=str(trade.get("setup_type") or "unknown"),
        timeframe=str(trade.get("timeframe") or "unknown"),
        entry_price=float(trade.get("entry_price") or 0.0),
        initial_stop=float(trade.get("sl_price") or 0.0),
        initial_target=float(trade.get("tp_price") or 0.0),
        initial_rr=float(trade.get("rr_ratio") or 0.0),
        mae_r=float(trade.get("max_adverse_r") or 0.0),
        mfe_r=float(trade.get("max_favorable_r") or 0.0),
        final_r=float(trade.get("pnl_r") or 0.0),
        duration_seconds=_duration_seconds(trade.get("timestamp"), trade.get("closed_at")),
        sl_modifications=sum(name == "SL Protected" for name in action_names),
        tp_modifications=sum(name == "TP Extended" for name in action_names),
        breakeven_activated="breakeven" in details,
        trailing_activated="trail" in details,
        partial_exit_used=any(name == "Partial Take Profit" for name in action_names),
        exit_reason=str(trade.get("exit_reason") or "broker_confirmed_exit"),
        policy_version=str(trade.get("policy_version") or ""),
        metadata={"source": "broker_confirmed_trade", "management_actions": action_names},
    )


def observations_from_backtest(result: Any) -> list[ManagementObservation]:
    """Adapt the existing causal BacktestEngine result; do not simulate anew."""
    observations: list[ManagementObservation] = []
    for trade in getattr(result, "trades", []) or []:
        entry_time = getattr(trade, "entry_time", None)
        exit_time = getattr(trade, "exit_time", None)
        duration = 0
        try:
            duration = max(0, int((exit_time - entry_time).total_seconds())) if exit_time is not None else 0
        except (AttributeError, TypeError):
            duration = 0
        events = list(getattr(trade, "management_events", []) or [])
        observations.append(ManagementObservation(
            ticket=None,
            symbol=str(getattr(trade, "symbol", "")),
            direction=str(getattr(trade, "direction", "")),
            setup_type="replayed_smc_setup",
            timeframe=str(getattr(result, "timeframe", "unknown")),
            entry_price=float(getattr(trade, "entry_price", 0.0) or 0.0),
            initial_stop=float(getattr(trade, "initial_stop", 0.0) or 0.0),
            initial_target=float(getattr(trade, "initial_target", getattr(trade, "take_profit", 0.0)) or 0.0),
            initial_rr=float(getattr(trade, "rr_ratio", 0.0) or 0.0),
            mae_r=float(getattr(trade, "max_adverse_r", 0.0) or 0.0),
            mfe_r=float(getattr(trade, "max_favorable_r", 0.0) or 0.0),
            final_r=float(getattr(trade, "rr_result", 0.0) or 0.0),
            duration_seconds=duration,
            sl_modifications=int(getattr(trade, "sl_modifications", 0) or 0),
            tp_modifications=int(getattr(trade, "tp_modifications", 0) or 0),
            breakeven_activated=bool(getattr(trade, "breakeven_activated", False)),
            trailing_activated=bool(getattr(trade, "trailing_activated", False)),
            partial_exit_used=bool(getattr(trade, "partial_closed", False)),
            exit_reason=str(getattr(trade, "exit_reason", "")),
            policy_version="replay",
            metadata={"source": "causal_backtest_replay", "management_events": events},
        ))
    return observations


def summarize_management(observations: Iterable[ManagementObservation]) -> dict[str, Any]:
    """Calculate descriptive TP/SL evidence without an execution decision."""
    rows = list(observations)
    evaluation = PolicyEvaluator.evaluate([{"pnl_r": row.final_r} for row in rows]).to_dict()
    distribution = completed_outcome_statistics([
        {
            "pnl_r": row.final_r,
            "mae_r": row.mae_r,
            "mfe_r": row.mfe_r,
            "target_r": row.initial_rr,
        }
        for row in rows
    ])
    return {
        "sample_size": len(rows),
        "expectancy_r": evaluation["expectancy_r"],
        "profit_factor": evaluation["profit_factor"],
        "win_rate": evaluation["win_rate"],
        "max_drawdown_r": evaluation["max_drawdown_r"],
        "average_mae_r": mean([row.mae_r for row in rows]) if rows else 0.0,
        "average_mfe_r": mean([row.mfe_r for row in rows]) if rows else 0.0,
        "average_duration_seconds": mean([row.duration_seconds for row in rows]) if rows else 0.0,
        "sl_modifications": sum(row.sl_modifications for row in rows),
        "tp_modifications": sum(row.tp_modifications for row in rows),
        "breakeven_activations": sum(row.breakeven_activated for row in rows),
        "trailing_activations": sum(row.trailing_activated for row in rows),
        "partial_exits": sum(row.partial_exit_used for row in rows),
        **distribution,
        "basis": "broker_confirmed_or_causal_replay_observations",
    }


def replay_existing_management_policy(
    *, settings: Any, candles: Any, htf_candles: list[Any], symbol: str, timeframe: str, policy: Any,
    initial_balance: float = 10_000.0,
) -> tuple[Any, list[ManagementObservation], dict[str, Any]]:
    """Replay a policy via the existing causal SMC/TP/SL engine.

    The function is intentionally synchronous because ``BacktestEngine``
    operates on candles already obtained from the broker-history provider. It
    is a pure research operation and cannot contact MT5 or submit an order.
    """
    from backtest.engine import BacktestEngine

    result = BacktestEngine(settings=settings, initial_balance=initial_balance, policy=policy).run(
        candles, htf_candles, symbol, timeframe
    )
    observations = observations_from_backtest(result)
    return result, observations, summarize_management(observations)


__all__ = [
    "ManagementObservation", "observation_from_broker_trade", "observations_from_backtest",
    "replay_existing_management_policy", "summarize_management",
]
