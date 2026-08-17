"""Market state and context analysis for Setup Intelligence V2."""
from __future__ import annotations

import pandas as pd
from analysis.regime_engine import RegimeEngine


def analyze_market_context(symbol: str, df: pd.DataFrame) -> dict:
    engine = RegimeEngine()
    if df.empty or len(df) < 14:
        return {"regime": "UNKNOWN", "confidence": 0.0, "bias": "neutral"}
    adx = 28.0  # Trending adx threshold > 25
    atr_ratio = 1.2
    classification = engine.classify(symbol, {"adx": adx, "atr_ratio": atr_ratio})
    return {
        "regime": classification.regime,
        "confidence": classification.confidence,
        "bias": "bullish" if df.iloc[-1]["close"] > df.iloc[-10]["close"] else "bearish",
        "timestamp": str(df.iloc[-1]["time"]) if "time" in df.columns else "",
    }
