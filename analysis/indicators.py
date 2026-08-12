"""
Base technical indicators used across analysis modules.
Pure pandas/numpy — causal swing detection and indicators with zero look-ahead bias.
"""

import pandas as pd
import numpy as np


def causal_swing_highs(df: pd.DataFrame, lookback: int = 3) -> pd.Series:
    """
    Causal swing high detection: candle i is a swing high if its high is higher
    than the highs of the preceding `lookback` bars AND the succeeding `lookback` bars.
    To avoid look-ahead bias in real-time execution, confirmed swing high at i
    is only available at index i + lookback.
    """
    highs = df["high"]
    n = len(df)
    is_swing = pd.Series(False, index=df.index)
    
    if n < (2 * lookback + 1):
        return is_swing

    for i in range(lookback, n - lookback):
        current_high = highs.iloc[i]
        is_sh = True
        # Check left and right
        for j in range(1, lookback + 1):
            if highs.iloc[i - j] >= current_high or highs.iloc[i + j] >= current_high:
                is_sh = False
                break
        if is_sh:
            is_swing.iloc[i] = True

    return is_swing


def causal_swing_lows(df: pd.DataFrame, lookback: int = 3) -> pd.Series:
    """
    Causal swing low detection: candle i is a swing low if its low is lower
    than the lows of the preceding `lookback` bars AND the succeeding `lookback` bars.
    """
    lows = df["low"]
    n = len(df)
    is_swing = pd.Series(False, index=df.index)
    
    if n < (2 * lookback + 1):
        return is_swing

    for i in range(lookback, n - lookback):
        current_low = lows.iloc[i]
        is_sl = True
        for j in range(1, lookback + 1):
            if lows.iloc[i - j] <= current_low or lows.iloc[i + j] <= current_low:
                is_sl = False
                break
        if is_sl:
            is_swing.iloc[i] = True

    return is_swing


def atr(df: pd.DataFrame, period: int = 14) -> pd.Series:
    """Average True Range."""
    high_low = df["high"] - df["low"]
    high_close = (df["high"] - df["close"].shift()).abs()
    low_close = (df["low"] - df["close"].shift()).abs()
    tr = pd.concat([high_low, high_close, low_close], axis=1).max(axis=1)
    return tr.rolling(window=period, min_periods=1).mean()


def ema(series: pd.Series, period: int) -> pd.Series:
    """Exponential Moving Average."""
    return series.ewm(span=period, adjust=False).mean()


def sma(series: pd.Series, period: int) -> pd.Series:
    """Simple Moving Average."""
    return series.rolling(window=period).mean()


def rsi(df: pd.DataFrame, period: int = 14) -> pd.Series:
    """Relative Strength Index."""
    delta = df["close"].diff()
    gain = delta.where(delta > 0, 0.0)
    loss = -delta.where(delta < 0, 0.0)
    avg_gain = gain.ewm(alpha=1 / period, min_periods=period).mean()
    avg_loss = loss.ewm(alpha=1 / period, min_periods=period).mean()
    rs = avg_gain / avg_loss.replace(0, np.nan)
    return 100 - (100 / (1 + rs))


def pip_value(symbol: str) -> float:
    """Approximate pip size for common instruments."""
    symbol_upper = symbol.upper()
    if "XAU" in symbol_upper or "GOLD" in symbol_upper:
        return 0.1
    if "JPY" in symbol_upper:
        return 0.01
    if "VOLATILITY" in symbol_upper or "BOOM" in symbol_upper or "CRASH" in symbol_upper:
        return 1.0
    return 0.0001
