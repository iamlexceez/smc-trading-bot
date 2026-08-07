"""
Real market data provider — replaces synthetic data in paper mode.

Sources (in priority order):
1. MetaTrader 5 (if connected) — real broker data, supports all instruments
2. Twelve Data API (free tier: 8 req/min, 800/day) — forex + gold
3. yfinance (free, no key) — forex + gold fallback
4. Synthetic data (last resort)

Get a free Twelve Data API key: https://twelvedata.com/pricing
"""

from __future__ import annotations

import os
import logging
import asyncio
from datetime import datetime, timedelta
from typing import Optional
import aiohttp

import pandas as pd
import numpy as np

logger = logging.getLogger(__name__)

TWELVE_DATA_KEY = os.getenv("TWELVE_DATA_API_KEY", "")
TWELVE_DATA_BASE = "https://api.twelvedata.com"

# Symbol mapping: our symbol → Twelve Data format
TD_SYMBOL_MAP = {
    "EURUSD": "EUR/USD",
    "GBPUSD": "GBP/USD",
    "USDJPY": "USD/JPY",
    "AUDUSD": "AUD/USD",
    "USDCAD": "USD/CAD",
    "USDCHF": "USD/CHF",
    "NZDUSD": "NZD/USD",
    "XAUUSD": "XAU/USD",
    "EURGBP": "EUR/GBP",
    "EURJPY": "EUR/JPY",
    "GBPJPY": "GBP/JPY",
    "XAGUSD": "XAG/USD",
}

# Symbol mapping: our symbol → yfinance format
YF_SYMBOL_MAP = {
    "EURUSD": "EURUSD=X",
    "GBPUSD": "GBPUSD=X",
    "USDJPY": "USDJPY=X",
    "AUDUSD": "AUDUSD=X",
    "USDCAD": "USDCAD=X",
    "USDCHF": "USDCHF=X",
    "NZDUSD": "NZDUSD=X",
    "XAUUSD": "GC=F",  # Gold futures
    "EURGBP": "EURGBP=X",
    "EURJPY": "EURJPY=X",
    "GBPJPY": "GBPJPY=X",
    "XAGUSD": "SI=F",  # Silver futures
}

TIMEFRAME_MAP = {
    "M1": "1min", "M5": "5min", "M15": "15min", "M30": "30min",
    "H1": "1h", "H4": "4h", "D1": "1day", "W1": "1week",
}

YF_INTERVAL_MAP = {
    "M1": "1m", "M5": "5m", "M15": "15m", "M30": "30m",
    "H1": "60m", "H4": "1h", "D1": "1d", "W1": "1wk",
}


class DataProvider:
    """Multi-source market data provider."""

    def __init__(self):
        self._td_session: Optional[aiohttp.ClientSession] = None
        self._mt5_available = False
        self._cache: dict[str, pd.DataFrame] = {}
        self._cache_time: dict[str, datetime] = {}
        self._cache_ttl = timedelta(minutes=2)  # Cache data for 2 minutes

    async def init(self):
        """Initialize connections."""
        # Check MT5 availability
        try:
            import MetaTrader5 as mt5
            if mt5.initialize():
                self._mt5_available = True
                logger.info("DataProvider: MT5 available")
                mt5.shutdown()
        except (ImportError, Exception):
            logger.info("DataProvider: MT5 not available, using API sources")

        # Create HTTP session for Twelve Data
        if TWELVE_DATA_KEY:
            self._td_session = aiohttp.ClientSession()
            logger.info("DataProvider: Twelve Data API configured")

    async def close(self):
        if self._td_session:
            await self._td_session.close()

    def is_synthetic(self, symbol: str) -> bool:
        """Check if a symbol is only available via synthetic data (e.g., synthetic indices)."""
        symbol_upper = symbol.upper()
        return any(x in symbol_upper for x in ["VOLATILITY", "BOOM", "CRASH", "STEP", "JUMP", "BULLY", "BEAR"])

    async def get_candles(
        self, symbol: str, timeframe: str, count: int = 200
    ) -> pd.DataFrame:
        """
        Fetch OHLCV candles. Tries MT5 → Twelve Data → yfinance → synthetic.
        Returns DataFrame with columns: time, open, high, low, close, tick_volume.
        """
        cache_key = f"{symbol}_{timeframe}_{count}"
        now = datetime.utcnow()

        # Check cache
        if cache_key in self._cache:
            if now - self._cache_time[cache_key] < self._cache_ttl:
                return self._cache[cache_key]

        # Try MT5
        if self._mt5_available:
            df = await self._fetch_mt5(symbol, timeframe, count)
            if df is not None and not df.empty:
                self._cache[cache_key] = df
                self._cache_time[cache_key] = now
                return df

        # Try Twelve Data
        if self._td_session and TWELVE_DATA_KEY:
            df = await self._fetch_twelvedata(symbol, timeframe, count)
            if df is not None and not df.empty:
                self._cache[cache_key] = df
                self._cache_time[cache_key] = now
                return df

        # Try yfinance
        if not self.is_synthetic(symbol):
            df = await self._fetch_yfinance(symbol, timeframe, count)
            if df is not None and not df.empty:
                self._cache[cache_key] = df
                self._cache_time[cache_key] = now
                return df

        # Last resort: synthetic data
        logger.warning(f"DataProvider: Using synthetic data for {symbol} {timeframe}")
        df = self._generate_synthetic(symbol, count)
        self._cache[cache_key] = df
        self._cache_time[cache_key] = now
        return df

    async def get_historical(
        self, symbol: str, timeframe: str, start: datetime, end: datetime
    ) -> pd.DataFrame:
        """Fetch historical data for backtesting."""
        # MT5 historical
        if self._mt5_available:
            df = await self._fetch_mt5_range(symbol, timeframe, start, end)
            if df is not None and not df.empty:
                return df

        # Twelve Data historical (supports larger ranges)
        if self._td_session and TWELVE_DATA_KEY:
            df = await self._fetch_twelvedata_range(symbol, timeframe, start, end)
            if df is not None and not df.empty:
                return df

        # yfinance historical
        if not self.is_synthetic(symbol):
            df = await self._fetch_yfinance_range(symbol, timeframe, start, end)
            if df is not None and not df.empty:
                return df

        # Synthetic for backtesting (only for synthetic indices)
        logger.warning(f"DataProvider: No historical data for {symbol}, generating synthetic")
        days = (end - start).days
        bars = max(count_from_tf(timeframe, days), 200)
        return self._generate_synthetic(symbol, bars)

    # ─── MT5 ────────────────────────────────────────────────

    async def _fetch_mt5(self, symbol: str, timeframe: str, count: int) -> Optional[pd.DataFrame]:
        try:
            import MetaTrader5 as mt5
            if not mt5.initialize():
                return None
            tf_const = getattr(mt5, f"TIMEFRAME_{timeframe}", mt5.TIMEFRAME_M15)
            rates = mt5.copy_rates_from_pos(symbol, tf_const, 0, count)
            mt5.shutdown()
            if rates is None or len(rates) == 0:
                return None
            df = pd.DataFrame(rates)
            df["time"] = pd.to_datetime(df["time"], unit="s")
            return df
        except Exception as e:
            logger.debug(f"MT5 fetch failed: {e}")
            return None

    async def _fetch_mt5_range(self, symbol: str, timeframe: str, start: datetime, end: datetime) -> Optional[pd.DataFrame]:
        try:
            import MetaTrader5 as mt5
            if not mt5.initialize():
                return None
            tf_const = getattr(mt5, f"TIMEFRAME_{timeframe}", mt5.TIMEFRAME_M15)
            rates = mt5.copy_rates_range(symbol, tf_const, start, end)
            mt5.shutdown()
            if rates is None or len(rates) == 0:
                return None
            df = pd.DataFrame(rates)
            df["time"] = pd.to_datetime(df["time"], unit="s")
            return df
        except Exception as e:
            logger.debug(f"MT5 historical fetch failed: {e}")
            return None

    # ─── Twelve Data ───────────────────────────────────────

    async def _fetch_twelvedata(self, symbol: str, timeframe: str, count: int) -> Optional[pd.DataFrame]:
        td_symbol = TD_SYMBOL_MAP.get(symbol.upper(), symbol)
        interval = TIMEFRAME_MAP.get(timeframe, "15min")

        params = {
            "symbol": td_symbol,
            "interval": interval,
            "outputsize": str(count),
            "apikey": TWELVE_DATA_KEY,
            "format": "JSON",
        }

        try:
            async with self._td_session.get(f"{TWELVE_DATA_BASE}/time_series", params=params) as resp:
                if resp.status != 200:
                    return None
                data = await resp.json()
                if "values" not in data:
                    return None

                values = data["values"]
                df = pd.DataFrame(values)
                df["time"] = pd.to_datetime(df["datetime"])
                df = df.rename(columns={
                    "open": "open", "high": "high", "low": "low",
                    "close": "close", "volume": "tick_volume"
                })
                df["open"] = df["open"].astype(float)
                df["high"] = df["high"].astype(float)
                df["low"] = df["low"].astype(float)
                df["close"] = df["close"].astype(float)
                if "tick_volume" not in df.columns:
                    df["tick_volume"] = 0
                df = df.sort_values("time").reset_index(drop=True)
                return df[["time", "open", "high", "low", "close", "tick_volume"]]
        except Exception as e:
            logger.debug(f"Twelve Data fetch failed: {e}")
            return None

    async def _fetch_twelvedata_range(self, symbol: str, timeframe: str, start: datetime, end: datetime) -> Optional[pd.DataFrame]:
        td_symbol = TD_SYMBOL_MAP.get(symbol.upper(), symbol)
        interval = TIMEFRAME_MAP.get(timeframe, "1day")

        params = {
            "symbol": td_symbol,
            "interval": interval,
            "start_date": start.strftime("%Y-%m-%d %H:%M:%S"),
            "end_date": end.strftime("%Y-%m-%d %H:%M:%S"),
            "apikey": TWELVE_DATA_KEY,
            "format": "JSON",
            "outputsize": "5000",
        }

        try:
            async with self._td_session.get(f"{TWELVE_DATA_BASE}/time_series", params=params) as resp:
                if resp.status != 200:
                    return None
                data = await resp.json()
                if "values" not in data:
                    return None
                values = data["values"]
                df = pd.DataFrame(values)
                df["time"] = pd.to_datetime(df["datetime"])
                df["open"] = df["open"].astype(float)
                df["high"] = df["high"].astype(float)
                df["low"] = df["low"].astype(float)
                df["close"] = df["close"].astype(float)
                df["tick_volume"] = 0
                df = df.sort_values("time").reset_index(drop=True)
                return df[["time", "open", "high", "low", "close", "tick_volume"]]
        except Exception as e:
            logger.debug(f"Twelve Data historical fetch failed: {e}")
            return None

    # ─── yfinance ───────────────────────────────────────────

    async def _fetch_yfinance(self, symbol: str, timeframe: str, count: int) -> Optional[pd.DataFrame]:
        yf_symbol = YF_SYMBOL_MAP.get(symbol.upper())
        if not yf_symbol:
            return None
        interval = YF_INTERVAL_MAP.get(timeframe, "15m")

        # yfinance interval limits: 1m=7d, 5m/15m/30m=60d, 60m=730d, 1d=max
        period_map = {"1m": "5d", "5m": "30d", "15m": "30d", "30m": "30d", "60m": "365d", "1h": "365d", "1d": "10y", "1wk": "10y"}
        period = period_map.get(interval, "30d")

        try:
            import yfinance as yf
            ticker = yf.Ticker(yf_symbol)
            hist = ticker.history(period=period, interval=interval)
            if hist.empty:
                return None
            hist = hist.tail(count)
            df = pd.DataFrame({
                "time": hist.index,
                "open": hist["Open"].values,
                "high": hist["High"].values,
                "low": hist["Low"].values,
                "close": hist["Close"].values,
                "tick_volume": hist["Volume"].values,
            })
            df = df.reset_index(drop=True)
            return df
        except Exception as e:
            logger.debug(f"yfinance fetch failed: {e}")
            return None

    async def _fetch_yfinance_range(self, symbol: str, timeframe: str, start: datetime, end: datetime) -> Optional[pd.DataFrame]:
        yf_symbol = YF_SYMBOL_MAP.get(symbol.upper())
        if not yf_symbol:
            return None
        interval = YF_INTERVAL_MAP.get(timeframe, "1d")

        try:
            import yfinance as yf
            ticker = yf.Ticker(yf_symbol)
            hist = ticker.history(start=start.strftime("%Y-%m-%d"), end=end.strftime("%Y-%m-%d"), interval=interval)
            if hist.empty:
                return None
            df = pd.DataFrame({
                "time": hist.index,
                "open": hist["Open"].values,
                "high": hist["High"].values,
                "low": hist["Low"].values,
                "close": hist["Close"].values,
                "tick_volume": hist["Volume"].values,
            })
            return df.reset_index(drop=True)
        except Exception as e:
            logger.debug(f"yfinance historical fetch failed: {e}")
            return None

    # ─── Synthetic (last resort for synthetic indices) ──────

    def _generate_synthetic(self, symbol: str, count: int = 200) -> pd.DataFrame:
        np.random.seed(hash(symbol) % 2**32)
        symbol_upper = symbol.upper()

        if "XAU" in symbol_upper or "GOLD" in symbol_upper:
            base, vol = 2000.0, 2.0
        elif "JPY" in symbol_upper:
            base, vol = 150.0, 0.2
        elif "VOLATILITY" in symbol_upper:
            base, vol = 5000.0, 50.0
        elif "BOOM" in symbol_upper:
            base, vol = 2000.0, 5.0
        elif "CRASH" in symbol_upper:
            base, vol = 2000.0, 5.0
        else:
            base, vol = 1.1000, 0.001

        returns = np.random.normal(0, vol, count)
        prices = base * np.exp(np.cumsum(returns / base))
        timestamps = pd.date_range(end=datetime.now(), periods=count, freq="15min")

        df = pd.DataFrame({
            "time": timestamps,
            "open": prices,
            "high": prices + np.abs(np.random.normal(0, vol * 0.5, count)),
            "low": prices - np.abs(np.random.normal(0, vol * 0.5, count)),
            "close": prices + np.random.normal(0, vol * 0.3, count),
            "tick_volume": np.random.randint(100, 10000, count),
        })
        df["high"] = df[["high", "open", "close"]].max(axis=1)
        df["low"] = df[["low", "open", "close"]].min(axis=1)
        return df

    async def get_price(self, symbol: str) -> tuple[float, float]:
        """Get current bid/ask for a symbol."""
        df = await self.get_candles(symbol, "M1", 5)
        if df.empty:
            return (0.0, 0.0)
        price = df.iloc[-1]["close"]
        spread = price * 0.0002
        return (price - spread, price + spread)


def count_from_tf(timeframe: str, days: int) -> int:
    """Convert days to bar count based on timeframe."""
    tf_minutes = {"M1": 1, "M5": 5, "M15": 15, "M30": 30, "H1": 60, "H4": 240, "D1": 1440, "W1": 10080}
    minutes = tf_minutes.get(timeframe, 15)
    bars = (days * 24 * 60) / minutes
    return int(min(bars, 50000))
