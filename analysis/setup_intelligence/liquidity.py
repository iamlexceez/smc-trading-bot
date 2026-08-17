"""Liquidity map and sweep detection for Setup Intelligence V2."""
from __future__ import annotations

import pandas as pd
from analysis.liquidity import build_liquidity_pools, detect_latest_sweep
from analysis.structure import detect_causal_swings


def map_liquidity(df: pd.DataFrame, timeframe: str) -> list:
    if df.empty or len(df) < 10:
        return []
    highs, lows = detect_causal_swings(df, lookback=2)
    pools = build_liquidity_pools(df, highs, lows, timeframe)
    mapped = []
    for pool in pools:
        mapped.append({
            "level": float(pool.level),
            "side": str(pool.side.value),
            "timeframe": timeframe,
            "strength": float(pool.strength),
            "status": "swept" if pool.swept else "active",
        })
    return mapped


def detect_sweep(df: pd.DataFrame, pools: list) -> dict:
    if df.empty:
        return {"swept": False}
    highs, lows = detect_causal_swings(df, lookback=2)
    raw_pools = build_liquidity_pools(df, highs, lows, "M15")
    sweep = detect_latest_sweep(df, raw_pools, direction="BUY")
    if not sweep:
        sweep = detect_latest_sweep(df, raw_pools, direction="SELL")
    if sweep:
        return {
            "swept": True,
            "level": sweep.pool.level,
            "side": sweep.pool.side.value,
            "index": sweep.index,
            "timestamp": sweep.timestamp,
        }
    return {"swept": False}
