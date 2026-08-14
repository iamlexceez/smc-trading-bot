"""
Institutional Market Structure Analysis — Causal, Non-Repainting SMC Engine.
Tracks protected highs/lows, BOS, CHoCH, Liquidity, Order Blocks, and FVGs without look-ahead bias.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Optional, List, Tuple

import pandas as pd
import numpy as np

from analysis.indicators import causal_swing_highs, causal_swing_lows, atr


class Trend(Enum):
    BULLISH = "bullish"
    BEARISH = "bearish"
    RANGING = "ranging"


class StructureEventType(Enum):
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
    confirmed: bool = True
    available_index: Optional[int] = None
    available_at: Optional[str] = None


@dataclass
class StructureEvent:
    event_type: StructureEventType
    price_level: float
    index: int
    timestamp: Optional[str] = None
    direction: str = "bullish"
    available_index: Optional[int] = None
    available_at: Optional[str] = None


@dataclass
class OrderBlock:
    index: int
    open_price: float
    close_price: float
    high: float
    low: float
    direction: str  # "bullish" or "bearish"
    mitigated: bool = False
    strength: float = 1.0
    available_index: Optional[int] = None
    available_at: Optional[str] = None


@dataclass
class FairValueGap:
    index: int
    top: float
    bottom: float
    direction: str  # "bullish" or "bearish"
    mitigated: bool = False
    strength: float = 1.0
    available_index: Optional[int] = None
    available_at: Optional[str] = None


@dataclass
class LiquidityPool:
    price: float
    type: str  # "buy-side" or "sell-side"
    index: int = 0
    swept: bool = False


@dataclass
class MarketStructure:
    trend: Trend = Trend.RANGING
    last_event: StructureEvent = field(default_factory=lambda: StructureEvent(StructureEventType.NONE, 0.0, 0))
    protected_high: float = 0.0
    protected_low: float = 0.0
    swing_highs: List[SwingPoint] = field(default_factory=list)
    swing_lows: List[SwingPoint] = field(default_factory=list)
    order_blocks: List[OrderBlock] = field(default_factory=list)
    fvgs: List[FairValueGap] = field(default_factory=list)
    liquidity_pools: List[LiquidityPool] = field(default_factory=list)
    current_zone: str = "equilibrium"
    premium_zone: Tuple[float, float] = (0.0, 0.0)
    discount_zone: Tuple[float, float] = (0.0, 0.0)
    equilibrium: float = 0.0

    @property
    def is_bullish(self) -> bool:
        return self.trend == Trend.BULLISH

    @property
    def is_bearish(self) -> bool:
        return self.trend == Trend.BEARISH


def detect_causal_swings(df: pd.DataFrame, lookback: int = 3) -> Tuple[List[SwingPoint], List[SwingPoint]]:
    """Detect causal swing highs and lows with zero look-ahead bias."""
    sh_mask = causal_swing_highs(df, lookback=lookback)
    sl_mask = causal_swing_lows(df, lookback=lookback)

    sh_list = []
    sl_list = []

    for source_index, is_swing in enumerate(sh_mask.to_numpy()):
        if not bool(is_swing):
            continue
        confirmation_index = source_index + lookback
        sh_list.append(SwingPoint(
            index=source_index,
            price=float(df.iloc[source_index]["high"]),
            type="high",
            timestamp=str(df.iloc[source_index]["time"]) if "time" in df.columns else None,
            available_index=confirmation_index,
            available_at=(str(df.iloc[confirmation_index]["time"]) if "time" in df.columns and confirmation_index < len(df) else None),
        ))

    for source_index, is_swing in enumerate(sl_mask.to_numpy()):
        if not bool(is_swing):
            continue
        confirmation_index = source_index + lookback
        sl_list.append(SwingPoint(
            index=source_index,
            price=float(df.iloc[source_index]["low"]),
            type="low",
            timestamp=str(df.iloc[source_index]["time"]) if "time" in df.columns else None,
            available_index=confirmation_index,
            available_at=(str(df.iloc[confirmation_index]["time"]) if "time" in df.columns and confirmation_index < len(df) else None),
        ))

    return sh_list, sl_list


def analyze_structure_causal(df: pd.DataFrame, lookback: int = 3) -> MarketStructure:
    """
    Perform fully causal, non-repainting market structure analysis.
    Evaluates protected highs/lows, candle-close BOS/CHOCH, order blocks, and FVGs.
    """
    sh_list, sl_list = detect_causal_swings(df, lookback=lookback)
    
    trend = Trend.RANGING
    last_event = StructureEvent(StructureEventType.NONE, 0.0, 0)
    protected_high = 0.0
    protected_low = 0.0

    if sh_list:
        protected_high = max(sh.price for sh in sh_list[-3:])
    if sl_list:
        protected_low = min(sl.price for sl in sl_list[-3:])

    # Determine trend from sequence of confirmed swings
    if len(sh_list) >= 2 and len(sl_list) >= 2:
        last_sh = sh_list[-1]
        prev_sh = sh_list[-2]
        last_sl = sl_list[-1]
        prev_sl = sl_list[-2]

        hh = last_sh.price > prev_sh.price
        hl = last_sl.price > prev_sl.price
        lh = last_sh.price < prev_sh.price
        ll = last_sl.price < prev_sl.price

        current_close = df.iloc[-1]["close"]

        if hh and hl:
            trend = Trend.BULLISH
            if current_close > prev_sh.price:
                last_event = StructureEvent(StructureEventType.BOS_BULLISH, prev_sh.price, last_sh.index, direction="bullish")
        elif lh and ll:
            trend = Trend.BEARISH
            if current_close < prev_sl.price:
                last_event = StructureEvent(StructureEventType.BOS_BEARISH, prev_sl.price, last_sl.index, direction="bearish")
        elif hh and ll:
            trend = Trend.RANGING
            if current_close > prev_sh.price:
                last_event = StructureEvent(StructureEventType.CHOCH_BULLISH, prev_sh.price, last_sh.index, direction="bullish")
        elif lh and hl:
            trend = Trend.RANGING
            if current_close < prev_sl.price:
                last_event = StructureEvent(StructureEventType.CHOCH_BEARISH, prev_sl.price, last_sl.index, direction="bearish")

    # Order Blocks (causal)
    obs = []
    atr_val = atr(df, 14)
    if len(df) > 5:
        for i in range(2, min(len(df) - 1, 15)):
            idx = len(df) - i
            candle = df.iloc[idx]
            impulse = df.iloc[idx + 1]
            avg_atr = atr_val.iloc[idx] if not np.isnan(atr_val.iloc[idx]) else 0.0001

            # Bullish OB
            if candle["close"] < candle["open"] and impulse["close"] > impulse["open"]:
                if abs(impulse["close"] - candle["close"]) > avg_atr:
                    obs.append(OrderBlock(
                        index=idx,
                        open_price=candle["open"],
                        close_price=candle["close"],
                        high=candle["high"],
                        low=candle["low"],
                        direction="bullish",
                        available_index=idx + 1,
                        available_at=(str(df.iloc[idx + 1]["time"]) if "time" in df.columns else None),
                    ))
                    break

            # Bearish OB
            if candle["close"] > candle["open"] and impulse["close"] < impulse["open"]:
                if abs(impulse["close"] - candle["close"]) > avg_atr:
                    obs.append(OrderBlock(
                        index=idx,
                        open_price=candle["open"],
                        close_price=candle["close"],
                        high=candle["high"],
                        low=candle["low"],
                        direction="bearish",
                        available_index=idx + 1,
                        available_at=(str(df.iloc[idx + 1]["time"]) if "time" in df.columns else None),
                    ))
                    break

    # FVGs (causal up to current bar)
    fvgs = []
    for i in range(max(2, len(df) - 15), len(df)):
        if i < 2:
            continue
        # Bullish FVG
        gap_top = df.iloc[i]["low"]
        gap_bottom = df.iloc[i - 2]["high"]
        if gap_top > gap_bottom:
            # Check if mitigated strictly by subsequent bars up to current
            mitigated = any(df.iloc[j]["low"] <= gap_top for j in range(i + 1, len(df)))
            if not mitigated:
                fvgs.append(FairValueGap(
                    index=i, top=gap_top, bottom=gap_bottom, direction="bullish", mitigated=False,
                    available_index=i, available_at=(str(df.iloc[i]["time"]) if "time" in df.columns else None),
                ))

        # Bearish FVG
        gap_top2 = df.iloc[i - 2]["low"]
        gap_bottom2 = df.iloc[i]["high"]
        if gap_top2 > gap_bottom2:
            mitigated = any(df.iloc[j]["high"] >= gap_bottom2 for j in range(i + 1, len(df)))
            if not mitigated:
                fvgs.append(FairValueGap(
                    index=i, top=gap_top2, bottom=gap_bottom2, direction="bearish", mitigated=False,
                    available_index=i, available_at=(str(df.iloc[i]["time"]) if "time" in df.columns else None),
                ))

    # Premium / Discount
    eq, p_zone, d_zone, zone_name = 0.0, (0.0, 0.0), (0.0, 0.0), "equilibrium"
    if sh_list and sl_list:
        rh = max(sp.price for sp in sh_list[-4:])
        rl = min(sp.price for sp in sl_list[-4:])
        eq = (rh + rl) / 2
        p_zone = (eq, rh)
        d_zone = (rl, eq)
        curr_price = df.iloc[-1]["close"]
        if curr_price > eq + (rh - rl) * 0.05:
            zone_name = "premium"
        elif curr_price < eq - (rh - rl) * 0.05:
            zone_name = "discount"

    # A BOS/CHOCH is only known when the current closed candle has confirmed it,
    # not on the older swing that supplied the level.
    last_event.available_index = len(df) - 1
    last_event.available_at = str(df.iloc[-1]["time"]) if "time" in df.columns else None

    return MarketStructure(
        trend=trend,
        last_event=last_event,
        protected_high=protected_high,
        protected_low=protected_low,
        swing_highs=sh_list,
        swing_lows=sl_list,
        order_blocks=obs,
        fvgs=fvgs,
        current_zone=zone_name,
        premium_zone=p_zone,
        discount_zone=d_zone,
        equilibrium=eq,
    )


# Alias for backward compatibility across modules
analyze_structure = analyze_structure_causal
