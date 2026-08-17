"""Trigger engine for Setup Intelligence V2."""
from __future__ import annotations

import pandas as pd


def check_trigger(df: pd.DataFrame, direction: str) -> dict[str, Any]:
    if df.empty:
        return {"triggered": False, "type": "none", "price": 0.0}
    bar = df.iloc[-1]
    return {
        "triggered": True,
        "type": "displacement_close",
        "price": float(bar["close"]),
    }
