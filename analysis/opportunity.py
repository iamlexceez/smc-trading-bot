"""Supporting market-context and opportunity-ranking calculations.

The module only describes known closed-candle context and orders already-valid
candidates.  It does not create entries, replace broker checks, or submit trades.
"""
from __future__ import annotations

from dataclasses import dataclass, field
import math
from typing import Any, Iterable

import numpy as np
import pandas as pd


def _finite(value: Any, default: float = 0.0) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    return number if math.isfinite(number) else default


def uncertainty_label(sample_size: int, conservative_ev: float, *, conflict: bool = False) -> str:
    """Classify uncertainty separately from technical setup score."""
    if conflict:
        return "CONFLICTED"
    if sample_size < 3:
        return "INSUFFICIENT_EVIDENCE"
    if sample_size < 10:
        return "HIGH"
    if sample_size < 30:
        return "MEDIUM"
    return "LOW" if conservative_ev > 0 else "MEDIUM"


def atr_series(frame: pd.DataFrame, period: int = 14) -> pd.Series:
    high_low = frame["high"] - frame["low"]
    high_close = (frame["high"] - frame["close"].shift()).abs()
    low_close = (frame["low"] - frame["close"].shift()).abs()
    return pd.concat([high_low, high_close, low_close], axis=1).max(axis=1).rolling(period).mean()


def rsi_series(frame: pd.DataFrame, period: int = 14) -> pd.Series:
    delta = frame["close"].diff()
    gains = delta.clip(lower=0).rolling(period).mean()
    losses = (-delta.clip(upper=0)).rolling(period).mean()
    rs = gains / losses.replace(0, np.nan)
    return 100.0 - (100.0 / (1.0 + rs))


def adx_series(frame: pd.DataFrame, period: int = 14) -> pd.Series:
    up = frame["high"].diff()
    down = -frame["low"].diff()
    plus_dm = up.where((up > down) & (up > 0), 0.0)
    minus_dm = down.where((down > up) & (down > 0), 0.0)
    tr = pd.concat([
        frame["high"] - frame["low"],
        (frame["high"] - frame["close"].shift()).abs(),
        (frame["low"] - frame["close"].shift()).abs(),
    ], axis=1).max(axis=1)
    atr = tr.rolling(period).mean().replace(0, np.nan)
    plus_di = 100.0 * plus_dm.rolling(period).mean() / atr
    minus_di = 100.0 * minus_dm.rolling(period).mean() / atr
    dx = 100.0 * (plus_di - minus_di).abs() / (plus_di + minus_di).replace(0, np.nan)
    return dx.rolling(period).mean()


def market_context(frame: pd.DataFrame) -> dict[str, Any]:
    """Return closed-candle ATR/ADX/RSI context and a non-entry regime label."""
    if frame.empty or len(frame) < 40:
        return {
            "regime": "UNKNOWN", "atr": 0.0, "atr_ratio": 1.0, "adx": 0.0,
            "rsi": 50.0, "momentum": 0.0, "efficiency": 0.0,
            "trend_persistence": 0.0, "range_ratio": 1.0, "displacement_ratio": 0.0,
            "confidence": "UNKNOWN",
        }
    data = frame.tail(500).reset_index(drop=True)
    atr = atr_series(data)
    adx = adx_series(data)
    rsi = rsi_series(data)
    current_atr = _finite(atr.iloc[-1])
    baseline = _finite(atr.dropna().tail(120).median(), current_atr)
    atr_ratio = current_atr / baseline if baseline > 0 else 1.0
    adx_value = _finite(adx.iloc[-1])
    rsi_value = _finite(rsi.iloc[-1], 50.0)
    close = data["close"]
    momentum = _finite((close.iloc[-1] - close.iloc[-6]) / max(abs(close.iloc[-6]), 1e-12) * 100.0)
    returns = close.diff().tail(20).dropna()
    total_path = float(returns.abs().sum())
    efficiency = abs(float(close.iloc[-1] - close.iloc[-min(20, len(close))])) / total_path if total_path > 0 else 0.0
    signs = np.sign(returns.to_numpy())
    directional_steps = signs[signs != 0]
    trend_persistence = (
        float(np.mean(directional_steps[1:] == directional_steps[:-1]))
        if len(directional_steps) >= 2 else 0.0
    )
    ranges = (data["high"] - data["low"])
    recent_range = _finite(ranges.tail(10).median())
    baseline_range = _finite(ranges.tail(100).median(), recent_range)
    range_ratio = recent_range / baseline_range if baseline_range > 0 else 1.0
    current_range = _finite(ranges.iloc[-1])
    displacement_ratio = current_range / current_atr if current_atr > 0 else 0.0
    # Regimes are descriptive classifications.  They never form a standalone
    # entry rule and are stored as context for policy evaluation.
    if atr_ratio < 0.75:
        regime = "COMPRESSION"
    elif atr_ratio > 1.35 and adx_value >= 20.0:
        regime = "EXPANSION"
    elif adx_value >= 25.0:
        regime = "TRENDING"
    elif adx_value <= 16.0:
        regime = "RANGING"
    elif abs(rsi_value - 50.0) >= 30.0 and abs(momentum) < 0.15:
        regime = "EXHAUSTION"
    else:
        regime = "TRANSITION"
    return {
        "regime": regime,
        "atr": current_atr,
        "atr_ratio": atr_ratio,
        "adx": adx_value,
        "rsi": rsi_value,
        "momentum": momentum,
        "efficiency": efficiency,
        "trend_persistence": trend_persistence,
        "range_ratio": range_ratio,
        "displacement_ratio": displacement_ratio,
        "confidence": "OBSERVED",
    }


@dataclass(frozen=True)
class Opportunity:
    symbol: str
    score: float
    classification: str
    rationale: tuple[str, ...]
    context: dict[str, Any]
    portfolio_conflict: float
    details: dict[str, Any] = field(default_factory=dict)


def rank_opportunities(
    candidates: Iterable[Any], *, profiles: dict[str, Any], contexts: dict[str, dict[str, Any]],
    historical: dict[str, dict[str, Any]], open_symbols: Iterable[str] = (),
) -> list[Opportunity]:
    """Rank existing eligible candidates without converting rankings into entries.

    The score combines already-recorded setup quality with observed regime and
    completed-outcome context.  It is intentionally transparent and the score
    does not bypass the active policy, broker validation, sizing, or portfolio
    checks that follow it.
    """
    open_set = {str(item) for item in open_symbols}
    ranked: list[Opportunity] = []
    for signal in candidates:
        symbol = str(signal.symbol)
        profile = profiles.get(symbol)
        context = dict(contexts.get(symbol) or {})
        evidence = dict(historical.get(symbol) or {})
        quality = max(0.0, min(100.0, _finite(getattr(signal, "score", 0.0))))
        selected_strategy = str(getattr(signal, "selected_strategy", "") or "unclassified")
        strategy_score = max(0.0, min(100.0, _finite(getattr(signal, "strategy_score", 0.0))))
        strategy_evidence = dict(getattr(signal, "strategy_evidence", {}) or {})
        adx = max(0.0, min(60.0, _finite(context.get("adx")))) / 60.0
        atr_ratio = _finite(context.get("atr_ratio"), 1.0)
        volatility_fit = max(0.0, 1.0 - min(abs(atr_ratio - 1.0), 1.0))
        momentum = min(abs(_finite(context.get("momentum"))) / 1.0, 1.0)
        evidence_basis = strategy_evidence if strategy_evidence else evidence
        expectancy = max(-1.0, min(1.0, _finite(evidence_basis.get("expectancy_r"))))
        sample = max(0, int(_finite(evidence_basis.get("sample_size"))))
        recent_expectancy = _finite(strategy_evidence.get("recent_expectancy_r"), expectancy)
        ci_low_raw = strategy_evidence.get("expectancy_ci95_low_r")
        ci_low = _finite(ci_low_raw, expectancy) if ci_low_raw is not None else expectancy
        conservative_ev = min(_finite(getattr(signal, "expected_value_r", None), expectancy), recent_expectancy, ci_low)
        evidence_strength = min(sample / 20.0, 1.0) * max(conservative_ev, 0.0)
        target_reach = strategy_evidence.get("target_reach_probability")
        target_reach_fit = max(0.0, min(1.0, _finite(target_reach, 0.5))) if target_reach is not None else 0.5
        profile_expectancy = max(0.0, min(1.0, _finite(getattr(profile, "expectancy_r", 0.0))))
        geometry_valid = bool(
            _finite(getattr(signal, "entry_price", 0.0)) > 0
            and _finite(getattr(signal, "stop_loss", 0.0)) > 0
            and _finite(getattr(signal, "take_profit", 0.0)) > 0
        )
        geometry_fit = 1.0 if geometry_valid else 0.0
        conflict = 1.0 if symbol in open_set else 0.0
        uncertainty = uncertainty_label(sample, conservative_ev, conflict=bool(conflict))
        score = quality * 0.45 + strategy_score * 0.10 + adx * 8.0 + volatility_fit * 7.0 + momentum * 6.0 + evidence_strength * 12.0 + target_reach_fit * 4.0 + profile_expectancy * 4.0 + geometry_fit * 4.0 - conflict * 18.0
        rationale = [f"setup quality {quality:.1f}/100", f"{context.get('regime', 'UNKNOWN').lower()} regime", f"strategy {selected_strategy} score {strategy_score:.1f}/100"]
        if evidence_strength > 0:
            rationale.append(f"positive conservative completed-outcome evidence n={sample}, EV={conservative_ev:+.2f}R")
        elif sample:
            rationale.append(f"inconclusive or uncertain completed-outcome evidence n={sample}, EV={conservative_ev:+.2f}R")
        else:
            rationale.append("no completed-outcome evidence yet; exploratory context")
        rationale.append("geometry present" if geometry_valid else "geometry incomplete; broker execution must withhold")
        if conflict:
            rationale.append("existing same-instrument exposure")
        classification = "BEST_OPPORTUNITY" if score >= 65.0 else ("GOOD_OPPORTUNITY" if score >= 45.0 else "WATCHLIST")
        details = {
            "instrument": symbol,
            "regime": context.get("regime", "UNKNOWN"),
            "strategy": selected_strategy,
            "direction": getattr(signal, "direction", ""),
            "timeframe": getattr(signal, "timeframe", ""),
            "htf_bias": list(getattr(signal, "htf_bias", []) or []),
            "entry": _finite(getattr(signal, "entry_price", None)),
            "stop_loss": _finite(getattr(signal, "stop_loss", None)),
            "take_profit": _finite(getattr(signal, "take_profit", None)),
            "rr": _finite(getattr(signal, "rr_ratio", None)),
            "setup_score": quality,
            "strategy_score": strategy_score,
            "historical_expectancy_r": strategy_evidence.get("expectancy_r"),
            "recent_expectancy_r": strategy_evidence.get("recent_expectancy_r"),
            "conservative_expected_value_r": conservative_ev,
            "expectancy_ci95_low_r": strategy_evidence.get("expectancy_ci95_low_r"),
            "expectancy_ci95_high_r": strategy_evidence.get("expectancy_ci95_high_r"),
            "target_reach_probability": target_reach,
            "confidence": strategy_evidence.get("confidence", "UNKNOWN"),
            "uncertainty": uncertainty,
            "evidence_stage": strategy_evidence.get("evidence_stage", "exploration"),
            "sample_size": strategy_evidence.get("sample_size", 0),
            "average_mae_r": strategy_evidence.get("average_mae_r"),
            "average_mfe_r": strategy_evidence.get("average_mfe_r"),
            "layering_suitability": bool(getattr(signal, "layering_suitable", False)),
            "portfolio_conflict": conflict,
            "execution_geometry_present": geometry_valid,
            "regime_transition": getattr(signal, "regime_transition", context.get("regime", "UNKNOWN")),
            "primary_thesis": dict(getattr(signal, "primary_thesis", {}) or {}),
            "alternative_theses": list(getattr(signal, "alternative_theses", []) or []),
            "thesis": list(rationale),
            "why_selected": "Pending comparative ranking; this field is filled after all eligible candidates are ordered.",
            "why_not_selected": "Pending comparative ranking; this field is filled after all eligible candidates are ordered.",
        }
        ranked.append(Opportunity(symbol, round(score, 4), classification, tuple(rationale), context, conflict, details))
    ranked.sort(key=lambda item: (-item.score, item.portfolio_conflict, item.symbol))
    if ranked:
        top = ranked[0]
        top_details = dict(top.details)
        top_details["why_selected"] = f"Ranked first at {top.score:.1f}; strongest eligible combination of setup quality, context, evidence, and execution geometry."
        top_details["why_not_selected"] = ""
        ranked[0] = Opportunity(top.symbol, top.score, "BEST_OPPORTUNITY", top.rationale, top.context, top.portfolio_conflict, top_details)
        for index, item in enumerate(ranked[1:], start=2):
            details = dict(item.details)
            gap = top.score - item.score
            details["why_selected"] = ""
            details["why_not_selected"] = f"Ranked #{index}, {gap:.1f} points behind the leading opportunity; review its uncertainty, evidence, and portfolio impact before selection."
            ranked[index - 1] = Opportunity(item.symbol, item.score, item.classification, item.rationale, item.context, item.portfolio_conflict, details)
    return ranked


__all__ = ["Opportunity", "adx_series", "atr_series", "market_context", "rank_opportunities", "rsi_series", "uncertainty_label"]
