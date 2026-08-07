"""
Base technical indicators used across analysis modules.
Pure pandas/numpy — no external TA library dependency for core SMC concepts.
"""

import pandas as pd
import numpy as np


def swing_highs(df: pd.DataFrame, lookback: int = 3) -> pd.Series:
    """Identify swing highs: a candle whose high is the highest within ±lookback bars."""
    highs = df["high"]
    return highs.rolling(window=2 * lookback + 1, center=True).max() == highs


def swing_lows(df: pd.DataFrame, lookback: int = 3) -> pd.Series:
    """Identify swing lows: a candle whose low is the lowest within ±lookback bars."""
    lows = df["low"]
    return lows.rolling(window=2 * lookback + 1, center=True).min() == lows


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
        return 0.1  # gold: 1 pip = $0.10 move
    if "JPY" in symbol_upper:
        return 0.01
    if "VOLATILITY" in symbol_upper or "BOOM" in symbol_upper or "CRASH" in symbol_upper:
        return 1.0  # synthetic indices: 1 point
    return 0.0001  # standard forex pairs


def detect_spread(df: pd.DataFrame) -> float:
    """Estimate current spread from the latest bar."""
    if df.empty:
        return 0.0
    last = df.iloc[-1]
    return last.get("spread", 0) if "spread" in df.columns else 0.0
