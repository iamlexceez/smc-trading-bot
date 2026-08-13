"""Measurable, closed-candle displacement detection.

Displacement is evaluated from the candle body/range and its true-range context.
It is intentionally independent from the scoring system so it can act as a hard
setup-validity condition.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

import pandas as pd

from analysis.indicators import atr


@dataclass(frozen=True)
class DisplacementResult:
    confirmed: bool
    direction: str
    index: int
    timestamp: Optional[str]
    body_ratio: float
    range_ratio: float
    candle_range: float
    average_range: float
    detail: str


def detect_displacement(
    df: pd.DataFrame,
    direction: str,
    body_ratio_min: float = 0.60,
    range_ratio_min: float = 1.20,
    lookback_bars: int = 4,
    atr_period: int = 20,
) -> DisplacementResult:
    """Find the latest closed candle that qualifies as directional displacement.

    The function only inspects the DataFrame supplied by the caller. In a live
    run that is the closed-candle history; in a backtest it is the slice ending
    at the simulated timestamp. Consequently, it does not need future candles.
    """
    empty = DisplacementResult(
        confirmed=False,
        direction=direction.upper(),
        index=-1,
        timestamp=None,
        body_ratio=0.0,
        range_ratio=0.0,
        candle_range=0.0,
        average_range=0.0,
        detail="No qualifying displacement candle",
    )
    if df.empty or len(df) < max(3, atr_period):
        return empty

    requested_direction = direction.upper()
    average_ranges = atr(df, atr_period)
    start_index = max(0, len(df) - lookback_bars)

    for index in range(len(df) - 1, start_index - 1, -1):
        candle = df.iloc[index]
        candle_range = float(candle["high"] - candle["low"])
        average_range = float(average_ranges.iloc[index])
        if candle_range <= 0 or average_range <= 0:
            continue

        body = abs(float(candle["close"] - candle["open"]))
        body_ratio = body / candle_range
        range_ratio = candle_range / average_range
        directional = (
            (requested_direction == "BUY" and float(candle["close"]) > float(candle["open"]))
            or (requested_direction == "SELL" and float(candle["close"]) < float(candle["open"]))
        )
        if directional and body_ratio >= body_ratio_min and range_ratio >= range_ratio_min:
            timestamp = str(candle["time"]) if "time" in df.columns else None
            return DisplacementResult(
                confirmed=True,
                direction=requested_direction,
                index=index,
                timestamp=timestamp,
                body_ratio=body_ratio,
                range_ratio=range_ratio,
                candle_range=candle_range,
                average_range=average_range,
                detail=(
                    f"{requested_direction} displacement: body={body_ratio:.2f} "
                    f"(min {body_ratio_min:.2f}), range={range_ratio:.2f}x "
                    f"(min {range_ratio_min:.2f}x)"
                ),
            )

    return empty


__all__ = ["DisplacementResult", "detect_displacement"]
