"""
Entry confirmation logic — wait for price to reach a zone and confirm before entering.

Confirmation methods (based on professional ICT/SMC methodology):
1. Zone retest: price must touch the S/D zone or order block
2. CHoCH on LTF: change of character on lower timeframe confirms reversal
3. Candle confirmation patterns: engulfing, pin bar, inside bar breakout
4. Displacement: strong impulse candle confirms institutional entry
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Optional

import pandas as pd
import numpy as np

from analysis.indicators import atr


class ConfirmationType(Enum):
    ENGULFING = "engulfing"
    PIN_BAR = "pin_bar"
    INSIDE_BAR_BREAKOUT = "inside_bar_breakout"
    DISPLACEMENT = "displacement"
    NONE = "none"


@dataclass
class ConfirmationResult:
    confirmed: bool
    method: ConfirmationType
    detail: str
    candle_index: int = -1
    available_index: int = -1
    available_at: str | None = None


def detect_engulfing(df: pd.DataFrame, direction: str, lookback: int = 3) -> ConfirmationResult:
    """
    Detect bullish/bearish engulfing pattern.
    Bullish: current candle engulfs previous bearish candle (body > prev body)
    Bearish: current candle engulfs previous bullish candle
    """
    if len(df) < 2:
        return ConfirmationResult(False, ConfirmationType.NONE, "Not enough data")

    for i in range(max(1, len(df) - lookback), len(df)):
        curr = df.iloc[i]
        prev = df.iloc[i - 1]

        if direction == "BUY":
            # Bullish engulfing: prev is bearish, curr is bullish, curr body engulfs prev body
            if (prev["close"] < prev["open"] and  # prev bearish
                curr["close"] > curr["open"] and  # curr bullish
                curr["close"] >= prev["open"] and  # curr close >= prev open
                curr["open"] <= prev["close"]):   # curr open <= prev close
                return ConfirmationResult(
                    True, ConfirmationType.ENGULFING,
                    f"Bullish engulfing at bar {i}", i, i,
                    (str(df.iloc[i]["time"]) if "time" in df.columns else None),
                )
        else:
            # Bearish engulfing
            if (prev["close"] > prev["open"] and  # prev bullish
                curr["close"] < curr["open"] and  # curr bearish
                curr["open"] >= prev["close"] and
                curr["close"] <= prev["open"]):
                return ConfirmationResult(
                    True, ConfirmationType.ENGULFING,
                    f"Bearish engulfing at bar {i}", i, i,
                    (str(df.iloc[i]["time"]) if "time" in df.columns else None),
                )

    return ConfirmationResult(False, ConfirmationType.NONE, "No engulfing pattern found")


def detect_pin_bar(df: pd.DataFrame, direction: str, lookback: int = 3) -> ConfirmationResult:
    """
    Detect pin bar (hammer/shooting star).
    Bullish pin: long lower wick, small body near top
    Bearish pin: long upper wick, small body near bottom
    """
    if len(df) < 1:
        return ConfirmationResult(False, ConfirmationType.NONE, "Not enough data")

    for i in range(max(0, len(df) - lookback), len(df)):
        candle = df.iloc[i]
        body = abs(candle["close"] - candle["open"])
        upper_wick = candle["high"] - max(candle["close"], candle["open"])
        lower_wick = min(candle["close"], candle["open"]) - candle["low"]
        total_range = candle["high"] - candle["low"]

        if total_range == 0:
            continue

        if direction == "BUY":
            # Bullish pin: lower wick >= 2x body, body in upper third
            if lower_wick >= body * 2 and lower_wick / total_range > 0.5:
                return ConfirmationResult(
                    True, ConfirmationType.PIN_BAR,
                    f"Bullish pin bar at bar {i}", i, i,
                    (str(df.iloc[i]["time"]) if "time" in df.columns else None),
                )
        else:
            # Bearish pin: upper wick >= 2x body, body in lower third
            if upper_wick >= body * 2 and upper_wick / total_range > 0.5:
                return ConfirmationResult(
                    True, ConfirmationType.PIN_BAR,
                    f"Bearish pin bar at bar {i}", i, i,
                    (str(df.iloc[i]["time"]) if "time" in df.columns else None),
                )

    return ConfirmationResult(False, ConfirmationType.NONE, "No pin bar found")


def detect_inside_bar_breakout(df: pd.DataFrame, direction: str, lookback: int = 5) -> ConfirmationResult:
    """
    Detect inside bar breakout.
    Inside bar: current candle's range is within previous candle's range.
    Breakout: next candle breaks out in trade direction.
    """
    if len(df) < 3:
        return ConfirmationResult(False, ConfirmationType.NONE, "Not enough data")

    # ``i`` is the inside bar and may be index 1. The following candle is the
    # closed confirmation, so do not inspect an inside bar without a next bar.
    for i in range(max(1, len(df) - lookback - 1), len(df) - 1):
        mother = df.iloc[i - 1]
        inside = df.iloc[i]

        # Check inside bar
        if inside["high"] <= mother["high"] and inside["low"] >= mother["low"]:
            # Check breakout direction
            if i + 1 < len(df):
                breakout = df.iloc[i + 1]
                if direction == "BUY" and breakout["close"] > mother["high"]:
                    return ConfirmationResult(
                        True, ConfirmationType.INSIDE_BAR_BREAKOUT,
                        f"Bullish inside bar breakout at bar {i+1}", i + 1, i + 1,
                        (str(df.iloc[i + 1]["time"]) if "time" in df.columns else None),
                    )
                elif direction == "SELL" and breakout["close"] < mother["low"]:
                    return ConfirmationResult(
                        True, ConfirmationType.INSIDE_BAR_BREAKOUT,
                        f"Bearish inside bar breakout at bar {i+1}", i + 1, i + 1,
                        (str(df.iloc[i + 1]["time"]) if "time" in df.columns else None),
                    )

    return ConfirmationResult(False, ConfirmationType.NONE, "No inside bar breakout found")


def detect_displacement(df: pd.DataFrame, direction: str, lookback: int = 5) -> ConfirmationResult:
    """
    Detect displacement — strong institutional impulse move.
    Displacement = candle body > 2x ATR, confirming institutional entry.
    """
    if len(df) < 15:
        return ConfirmationResult(False, ConfirmationType.NONE, "Not enough data for ATR")

    atr_val = atr(df, 14)

    for i in range(max(2, len(df) - lookback), len(df)):
        candle = df.iloc[i]
        body = abs(candle["close"] - candle["open"])
        avg_atr = atr_val.iloc[i] if not np.isnan(atr_val.iloc[i]) else 0.0001

        if body > avg_atr * 2.0:  # Strong displacement
            if direction == "BUY" and candle["close"] > candle["open"]:
                return ConfirmationResult(
                    True, ConfirmationType.DISPLACEMENT,
                    f"Bullish displacement (body={body:.5f}, ATR={avg_atr:.5f}) at bar {i}", i, i,
                    (str(df.iloc[i]["time"]) if "time" in df.columns else None),
                )
            elif direction == "SELL" and candle["close"] < candle["open"]:
                return ConfirmationResult(
                    True, ConfirmationType.DISPLACEMENT,
                    f"Bearish displacement (body={body:.5f}, ATR={avg_atr:.5f}) at bar {i}", i, i,
                    (str(df.iloc[i]["time"]) if "time" in df.columns else None),
                )

    return ConfirmationResult(False, ConfirmationType.NONE, "No displacement found")


def check_zone_retest(
    df: pd.DataFrame,
    zone_top: float,
    zone_bottom: float,
    direction: str,
    lookback: int = 10,
) -> bool:
    """
    Check if price has recently retested a zone (touched it within last N bars).
    """
    if len(df) < 1:
        return False

    recent = df.tail(lookback)
    if direction == "BUY":
        # For demand zone: price should have dipped into the zone
        return any(row["low"] <= zone_top for _, row in recent.iterrows())
    else:
        # For supply zone: price should have risen into the zone
        return any(row["high"] >= zone_bottom for _, row in recent.iterrows())


def get_confirmation(
    df: pd.DataFrame,
    direction: str,
    zone_top: float = 0,
    zone_bottom: float = 0,
    require_retest: bool = True,
    require_candle: bool = True,
    require_displacement: bool = False,
) -> ConfirmationResult:
    """
    Full entry confirmation check.
    Combines zone retest + candle pattern + optional displacement.

    Args:
        df: OHLCV data
        direction: "BUY" or "SELL"
        zone_top: Top of the zone to retest
        zone_bottom: Bottom of the zone to retest
        require_retest: Must price have touched the zone?
        require_candle: Must a confirmation candle be present?
        require_displacement: Must there be a displacement candle?
    """
    # 1. Zone retest
    if require_retest and zone_top > 0 and zone_bottom > 0:
        if not check_zone_retest(df, zone_top, zone_bottom, direction):
            return ConfirmationResult(
                False, ConfirmationType.NONE,
                f"Price has not retested the zone ({zone_bottom:.5f}-{zone_top:.5f})"
            )

    # 2. Displacement (optional but strong)
    if require_displacement:
        disp = detect_displacement(df, direction)
        if disp.confirmed:
            return disp

    # 3. Candle confirmation
    if require_candle:
        # Try engulfing first (strongest)
        result = detect_engulfing(df, direction)
        if result.confirmed:
            return result

        # Then pin bar
        result = detect_pin_bar(df, direction)
        if result.confirmed:
            return result

        # Then inside bar breakout
        result = detect_inside_bar_breakout(df, direction)
        if result.confirmed:
            return result

        return ConfirmationResult(
            False, ConfirmationType.NONE,
            "No confirmation candle pattern found (engulfing, pin bar, inside bar)"
        )

    # If no candle required, just return retest as confirmation
    return ConfirmationResult(
        True, ConfirmationType.NONE,
        "Zone retest confirmed" if require_retest else "No confirmation required"
    )
