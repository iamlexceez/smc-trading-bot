"""Location and POI engine for Setup Intelligence V2."""
from __future__ import annotations

import pandas as pd
from analysis.supply_demand import detect_sd_zones


def identify_poi(df: pd.DataFrame) -> dict:
    zones = detect_sd_zones(df, lookback=100)
    if zones:
        zone = zones[-1]
        return {
            "type": str(getattr(zone, "source", "supply_demand")),
            "price": float((zone.top + zone.bottom) / 2.0),
            "high": float(zone.top),
            "low": float(zone.bottom),
        }
    # Fallback POI from recent price action if no S/D zone detected
    if not df.empty:
        low = float(df["low"].iloc[-5:].min())
        high = float(df["high"].iloc[-5:].max())
        mid = (high + low) / 2.0
        return {
            "type": "fallback_swing_poi",
            "price": mid,
            "high": high,
            "low": low,
        }
    return {"type": "none", "price": 0.0, "high": 0.0, "low": 0.0}
