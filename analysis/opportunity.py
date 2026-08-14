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
        return {"regime": "UNKNOWN", "atr": 0.0, "atr_ratio": 1.0, "adx": 0.0, "rsi": 50.0, "momentum": 0.0, "confidence": "UNKNOWN"}
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
        expectancy = max(-1.0, min(1.0, _finite(evidence.get("expectancy_r"))))
        sample = max(0, int(_finite(evidence.get("sample_size"))))
        evidence_strength = min(sample / 20.0, 1.0) * max(expectancy, 0.0)
        profile_expectancy = max(0.0, min(1.0, _finite(getattr(profile, "expectancy_r", 0.0))))
        conflict = 1.0 if symbol in open_set else 0.0
        score = quality * 0.48 + strategy_score * 0.10 + adx * 10.0 + volatility_fit * 8.0 + momentum * 7.0 + evidence_strength * 12.0 + profile_expectancy * 5.0 - conflict * 18.0
        rationale = [f"setup quality {quality:.1f}/100", f"{context.get('regime', 'UNKNOWN').lower()} regime", f"strategy {selected_strategy} score {strategy_score:.1f}/100"]
        if evidence_strength > 0:
            rationale.append(f"positive completed-outcome evidence n={sample}")
        elif sample:
            rationale.append(f"inconclusive completed-outcome evidence n={sample}")
        else:
            rationale.append("no completed-outcome evidence yet")
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
            "confidence": strategy_evidence.get("confidence", "UNKNOWN"),
            "sample_size": strategy_evidence.get("sample_size", 0),
            "average_mae_r": strategy_evidence.get("average_mae_r"),
            "average_mfe_r": strategy_evidence.get("average_mfe_r"),
            "layering_suitability": bool(getattr(signal, "layering_suitable", False)),
            "portfolio_conflict": conflict,
            "thesis": list(rationale),
        }
        ranked.append(Opportunity(symbol, round(score, 4), classification, tuple(rationale), context, conflict, details))
    ranked.sort(key=lambda item: (-item.score, item.portfolio_conflict, item.symbol))
    if ranked:
        top = ranked[0]
        ranked[0] = Opportunity(top.symbol, top.score, "BEST_OPPORTUNITY", top.rationale, top.context, top.portfolio_conflict, top.details)
    return ranked


__all__ = ["Opportunity", "adx_series", "atr_series", "market_context", "rank_opportunities", "rsi_series"]
