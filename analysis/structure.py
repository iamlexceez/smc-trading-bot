"""
APA (Advanced Price Action) Market Structure Analysis.

Detects:
- Swing highs/lows → HH, HL, LH, LL sequence
- Break of Structure (BOS) — trend continuation
- Change of Character (CHoCH) — trend reversal
- Order Blocks (last opposite candle before impulse move)
- Fair Value Gaps (FVG / Imbalances)
- Liquidity pools (equal highs/lows, previous session H/L)
- Premium / Discount zones
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Optional

import pandas as pd
import numpy as np

from analysis.indicators import swing_highs, swing_lows, atr, ema


class Trend(Enum):
    BULLISH = "bullish"
    BEARISH = "bearish"
    RANGING = "ranging"


class StructureEvent(Enum):
    BOS_BULLISH = "bos_bullish"
    BOS_BEARISH = "bos_bearish"
    CHOCH_BULLISH = "choch_bullish"
    CHOCH_BEARISH = "choch_bearish"
    NONE = "none"


@dataclass
class SwingPoint:
    index: int
    price: float
    type: str  # "high" or "low"
    timestamp: Optional[str] = None


@dataclass
class OrderBlock:
    open_time: int
    open_price: float
    close_price: float
    high: float
    low: float
    direction: str  # "bullish" or "bearish"
    mitigated: bool = False
    index: int = 0


@dataclass
class FairValueGap:
    index: int
    top: float
    bottom: float
    direction: str  # "bullish" or "bearish"
    mitigated: bool = False


@dataclass
class LiquidityPool:
    price: float
    type: str  # "buy-side" (above) or "sell-side" (below)
    index: int = 0


@dataclass
class MarketStructure:
    trend: Trend = Trend.RANGING
    last_event: StructureEvent = StructureEvent.NONE
    swing_highs: list[SwingPoint] = field(default_factory=list)
    swing_lows: list[SwingPoint] = field(default_factory=list)
    order_blocks: list[OrderBlock] = field(default_factory=list)
    fvgs: list[FairValueGap] = field(default_factory=list)
    liquidity_pools: list[LiquidityPool] = field(default_factory=list)
    current_zone: str = "equilibrium"  # premium, discount, equilibrium
    premium_zone: tuple[float, float] = (0.0, 0.0)
    discount_zone: tuple[float, float] = (0.0, 0.0)
    equilibrium: float = 0.0

    @property
    def is_bullish(self) -> bool:
        return self.trend == Trend.BULLISH

    @property
    def is_bearish(self) -> bool:
        return self.trend == Trend.BEARISH


def detect_swing_points(df: pd.DataFrame, lookback: int = 3) -> tuple[list[SwingPoint], list[SwingPoint]]:
    """Detect swing highs and lows from OHLCV data."""
    sh_mask = swing_highs(df, lookback)
    sl_mask = swing_lows(df, lookback)

    swing_highs_list = []
    swing_lows_list = []

    for idx in df.index[sh_mask.fillna(False)]:
        swing_highs_list.append(SwingPoint(
            index=idx,
            price=df.loc[idx, "high"],
            type="high",
            timestamp=str(df.loc[idx, "time"]) if "time" in df.columns else None
        ))

    for idx in df.index[sl_mask.fillna(False)]:
        swing_lows_list.append(SwingPoint(
            index=idx,
            price=df.loc[idx, "low"],
            type="low",
            timestamp=str(df.loc[idx, "time"]) if "time" in df.columns else None
        ))

    return swing_highs_list, swing_lows_list


def detect_trend_and_structure(
    swing_highs: list[SwingPoint],
    swing_lows: list[SwingPoint],
) -> tuple[Trend, StructureEvent]:
    """
    Determine trend and last structural event from swing point sequence.
    BOS = continuation, CHoCH = reversal.
    We need at least 3 swing points of each type to detect a prior trend.
    """
    if len(swing_highs) < 2 or len(swing_lows) < 2:
        return Trend.RANGING, StructureEvent.NONE

    last_sh = swing_highs[-1]
    prev_sh = swing_highs[-2]
    last_sl = swing_lows[-1]
    prev_sl = swing_lows[-2]

    # Determine prior trend from earlier swings (if available)
    prior_trend = Trend.RANGING
    if len(swing_highs) >= 3 and len(swing_lows) >= 3:
        pp_sh = swing_highs[-3]
        pp_sl = swing_lows[-3]
        if prev_sh.price > pp_sh.price and prev_sl.price > pp_sl.price:
            prior_trend = Trend.BULLISH
        elif prev_sh.price < pp_sh.price and prev_sl.price < pp_sl.price:
            prior_trend = Trend.BEARISH

    # Current structure
    hh = last_sh.price > prev_sh.price
    hl = last_sl.price > prev_sl.price
    lh = last_sh.price < prev_sh.price
    ll = last_sl.price < prev_sl.price

    if hh and hl:
        trend = Trend.BULLISH
        event = StructureEvent.BOS_BULLISH
    elif lh and ll:
        trend = Trend.BEARISH
        event = StructureEvent.BOS_BEARISH
    elif hh and ll:
        # Divergence — potential reversal from bearish to bullish
        trend = Trend.RANGING
        event = StructureEvent.CHOCH_BULLISH if prior_trend == Trend.BEARISH else StructureEvent.NONE
    elif lh and hl:
        # Divergence — potential reversal from bullish to bearish
        trend = Trend.RANGING
        event = StructureEvent.CHOCH_BEARISH if prior_trend == Trend.BULLISH else StructureEvent.NONE
    else:
        trend = Trend.RANGING
        event = StructureEvent.NONE

    return trend, event


def detect_order_blocks(df: pd.DataFrame, lookback: int = 10) -> list[OrderBlock]:
    """
    Detect order blocks: the last opposite-colored candle before a strong impulse move.
    Bullish OB = last bearish candle before bullish impulse.
    Bearish OB = last bullish candle before bearish impulse.
    """
    obs = []
    atr_val = atr(df, 14)

    for i in range(2, min(len(df) - 1, lookback + 2)):
        idx = len(df) - i
        if idx < 1:
            break

        candle = df.iloc[idx]
        prev = df.iloc[idx - 1]
        impulse = df.iloc[idx + 1] if idx + 1 < len(df) else None
        if impulse is None:
            continue

        move = abs(impulse["close"] - candle["close"])
        avg_atr = atr_val.iloc[idx] if not np.isnan(atr_val.iloc[idx]) else 0.0001

        # Bullish OB: bearish candle followed by strong bullish impulse
        if candle["close"] < candle["open"] and impulse["close"] > impulse["open"]:
            if move > avg_atr * 1.2:
                obs.append(OrderBlock(
                    open_time=idx,
                    open_price=candle["open"],
                    close_price=candle["close"],
                    high=candle["high"],
                    low=candle["low"],
                    direction="bullish",
                    index=idx,
                ))
                break  # Most recent OB only

    for i in range(2, min(len(df) - 1, lookback + 2)):
        idx = len(df) - i
        if idx < 1:
            break

        candle = df.iloc[idx]
        impulse = df.iloc[idx + 1] if idx + 1 < len(df) else None
        if impulse is None:
            continue

        move = abs(impulse["close"] - candle["close"])
        avg_atr = atr_val.iloc[idx] if not np.isnan(atr_val.iloc[idx]) else 0.0001

        # Bearish OB: bullish candle followed by strong bearish impulse
        if candle["close"] > candle["open"] and impulse["close"] < impulse["open"]:
            if move > avg_atr * 1.2:
                obs.append(OrderBlock(
                    open_time=idx,
                    open_price=candle["open"],
                    close_price=candle["close"],
                    high=candle["high"],
                    low=candle["low"],
                    direction="bearish",
                    index=idx,
                ))
                break

    return obs


def detect_fvgs(df: pd.DataFrame, lookback: int = 10) -> list[FairValueGap]:
    """
    Detect Fair Value Gaps (imbalances / 3-candle patterns).
    Bullish FVG: candle[i-1].high < candle[i+1].low → gap between them.
    Bearish FVG: candle[i-1].low > candle[i+1].high → gap between them.
    """
    fvgs = []
    start = max(2, len(df) - lookback)

    for i in range(start, len(df)):
        if i < 2:
            continue

        # Bullish FVG
        gap_top = df.iloc[i]["low"]
        gap_bottom = df.iloc[i - 2]["high"]
        if gap_top > gap_bottom:
            # Check if mitigated (price returned to gap)
            mitigated = any(
                df.iloc[j]["low"] <= gap_top
                for j in range(i + 1, len(df))
            )
            fvgs.append(FairValueGap(
                index=i,
                top=gap_top,
                bottom=gap_bottom,
                direction="bullish",
                mitigated=mitigated,
            ))

        # Bearish FVG
        gap_top2 = df.iloc[i - 2]["low"]
        gap_bottom2 = df.iloc[i]["high"]
        if gap_top2 > gap_bottom2:
            mitigated = any(
                df.iloc[j]["high"] >= gap_bottom2
                for j in range(i + 1, len(df))
            )
            fvgs.append(FairValueGap(
                index=i,
                top=gap_top2,
                bottom=gap_bottom2,
                direction="bearish",
                mitigated=mitigated,
            ))

    return fvgs


def detect_liquidity_pools(
    swing_highs: list[SwingPoint],
    swing_lows: list[SwingPoint],
    tolerance: float = 0.0003,
) -> list[LiquidityPool]:
    """
    Detect liquidity pools: equal highs (buy-side) and equal lows (sell-side).
    These are areas where stop orders accumulate.
    """
    pools = []

    # Buy-side liquidity: equal highs
    for i in range(len(swing_highs) - 1):
        sh1 = swing_highs[i]
        sh2 = swing_highs[i + 1]
        if abs(sh1.price - sh2.price) / max(sh1.price, 1e-7) < tolerance:
            pools.append(LiquidityPool(
                price=max(sh1.price, sh2.price),
                type="buy-side",
                index=sh2.index,
            ))

    # Sell-side liquidity: equal lows
    for i in range(len(swing_lows) - 1):
        sl1 = swing_lows[i]
        sl2 = swing_lows[i + 1]
        if abs(sl1.price - sl2.price) / max(sl1.price, 1e-7) < tolerance:
            pools.append(LiquidityPool(
                price=min(sl1.price, sl2.price),
                type="sell-side",
                index=sl2.index,
            ))

    return pools


def compute_premium_discount(df: pd.DataFrame, swing_highs: list[SwingPoint], swing_lows: list[SwingPoint]) -> dict:
    """
    Compute premium/discount zones using the most recent significant range.
    Premium = upper 50% (sell zone), Discount = lower 50% (buy zone).
    """
    if not swing_highs or not swing_lows:
        return {
            "current_zone": "equilibrium",
            "premium_zone": (0.0, 0.0),
            "discount_zone": (0.0, 0.0),
            "equilibrium": 0.0,
        }

    range_high = max(sp.price for sp in swing_highs[-4:])
    range_low = min(sp.price for sp in swing_lows[-4:])
    eq = (range_high + range_low) / 2

    current_price = df.iloc[-1]["close"]
    if current_price > eq + (range_high - range_low) * 0.1:
        zone = "premium"
    elif current_price < eq - (range_high - range_low) * 0.1:
        zone = "discount"
    else:
        zone = "equilibrium"

    return {
        "current_zone": zone,
        "premium_zone": (eq, range_high),
        "discount_zone": (range_low, eq),
        "equilibrium": eq,
    }


def analyze_structure(df: pd.DataFrame, lookback: int = 3) -> MarketStructure:
    """
    Full market structure analysis on a single timeframe.
    Returns a MarketStructure object with all detected elements.
    """
    sh_list, sl_list = detect_swing_points(df, lookback=lookback)
    trend, event = detect_trend_and_structure(sh_list, sl_list)
    obs = detect_order_blocks(df)
    fvgs = detect_fvgs(df)
    pools = detect_liquidity_pools(sh_list, sl_list)
    pd_info = compute_premium_discount(df, sh_list, sl_list)

    return MarketStructure(
        trend=trend,
        last_event=event,
        swing_highs=sh_list,
        swing_lows=sl_list,
        order_blocks=obs,
        fvgs=[f for f in fvgs if not f.mitigated],  # Only unmitigated FVGs
        liquidity_pools=pools,
        current_zone=pd_info["current_zone"],
        premium_zone=pd_info["premium_zone"],
        discount_zone=pd_info["discount_zone"],
        equilibrium=pd_info["equilibrium"],
    )
