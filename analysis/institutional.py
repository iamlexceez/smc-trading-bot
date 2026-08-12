"""
Advanced Institutional Analysis for SMC Trading Bot.
Implements Fair Value Gaps (FVG) and Liquidity Sweep detection.
"""

import pandas as pd
import numpy as np
from dataclasses import dataclass
from enum import Enum

class InefficiencyType(Enum):
    FVG_BULLISH = "fvg_bullish"
    FVG_BEARISH = "fvg_bearish"
    NONE = "none"

@dataclass
class FVG:
    top: float
    bottom: float
    gap_type: InefficiencyType
    index: int

def detect_fvgs(df: pd.DataFrame, lookback: int = 50) -> list[FVG]:
    """Detect Fair Value Gaps in the recent price action."""
    fvgs = []
    if len(df) < 3:
        return fvgs
        
    # Check the last 'lookback' candles
    start_idx = max(1, len(df) - lookback)
    for i in range(start_idx, len(df) - 1):
        # Bullish FVG: Low of candle 3 > High of candle 1
        # [i-1, i, i+1]
        c1_high = df.iloc[i-1]['high']
        c3_low = df.iloc[i+1]['low']
        
        if c3_low > c1_high:
            fvgs.append(FVG(top=c3_low, bottom=c1_high, gap_type=InefficiencyType.FVG_BULLISH, index=i))
            
        # Bearish FVG: High of candle 3 < Low of candle 1
        c1_low = df.iloc[i-1]['low']
        c3_high = df.iloc[i+1]['high']
        
        if c3_high < c1_low:
            fvgs.append(FVG(top=c1_low, bottom=c3_high, gap_type=InefficiencyType.FVG_BEARISH, index=i))
            
    return fvgs

def detect_liquidity_sweeps(df: pd.DataFrame, lookback: int = 20) -> dict:
    """Detect if the current candle has swept liquidity of recent highs/lows."""
    if len(df) < lookback + 1:
        return {"bullish_sweep": False, "bearish_sweep": False}
        
    recent_data = df.iloc[-(lookback+1):-1]
    recent_high = recent_data['high'].max()
    recent_low = recent_data['low'].min()
    
    current_candle = df.iloc[-1]
    
    # Bearish Sweep: Current high > recent high AND current close < recent high
    bearish_sweep = current_candle['high'] > recent_high and current_candle['close'] < recent_high
    
    # Bullish Sweep: Current low < recent low AND current close > recent low
    bullish_sweep = current_candle['low'] < recent_low and current_candle['close'] > recent_low
    
    return {
        "bullish_sweep": bullish_sweep,
        "bearish_sweep": bearish_sweep,
        "recent_high": recent_high,
        "recent_low": recent_low
    }

def calculate_ote_levels(high: float, low: float, direction: str) -> dict:
    """Calculate Optimal Trade Entry (OTE) Fibonacci levels."""
    diff = high - low
    if direction == "BUY":
        return {
            "62.0": high - diff * 0.62,
            "70.5": high - diff * 0.705,
            "79.0": high - diff * 0.79
        }
    else:
        return {
            "62.0": low + diff * 0.62,
            "70.5": low + diff * 0.705,
            "79.0": low + diff * 0.79
        }
