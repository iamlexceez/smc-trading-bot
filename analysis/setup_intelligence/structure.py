"""Structure engine for Setup Intelligence V2."""
from __future__ import annotations

import pandas as pd
from analysis.structure import analyze_structure


def analyze_market_structure(df: pd.DataFrame) -> dict[str, Any]:
    structure = analyze_structure(df, lookback=3)
    event = structure.last_event
    return {
        "trend": structure.trend.value,
        "current_zone": structure.current_zone,
        "event_type": event.event_type.value,
        "index": int(event.index),
    }
