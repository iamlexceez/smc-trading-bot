"""Broker-only OHLCV provider for the Deriv MT5 execution system.

Trading and learning use only closed candles retrieved from the connected MT5
account. Missing broker data is an explicit unavailable condition; it is never
replaced with forex feeds, unrelated Gold futures, or generated prices.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Any, Optional

import pandas as pd

logger = logging.getLogger(__name__)

REQUIRED_COLUMNS = ("time", "open", "high", "low", "close", "tick_volume")


class DataProvider:
    """Retrieve broker-native closed candles with a short-lived in-memory cache."""

    def __init__(self, executor: Optional[Any] = None, cache_ttl_seconds: int = 20):
        self.executor = executor
        self._cache: dict[str, pd.DataFrame] = {}
        self._cache_time: dict[str, datetime] = {}
        self._cache_ttl = timedelta(seconds=max(0, cache_ttl_seconds))
        self._broker_available = False

    @staticmethod
    def empty_frame() -> pd.DataFrame:
        return pd.DataFrame(columns=list(REQUIRED_COLUMNS))

    async def init(self) -> bool:
        """Verify the configured execution backend can serve broker candles."""
        if self.executor is None:
            logger.error("Broker data is unavailable: no executor was supplied")
            return False
        try:
            self._broker_available = await self.executor.is_connected()
            if not self._broker_available:
                self._broker_available = await self.executor.connect()
        except Exception as exc:
            logger.error("Broker data initialization failed: %s", exc)
            self._broker_available = False
        if not self._broker_available:
            logger.error("Broker data is unavailable; scans will fail closed")
        return self._broker_available

    async def close(self) -> None:
        """Clear only local cache; executor lifecycle is owned by the application."""
        self._cache.clear()
        self._cache_time.clear()

    def _cache_get(self, key: str) -> Optional[pd.DataFrame]:
        cached = self._cache.get(key)
        if cached is not None and datetime.utcnow() - self._cache_time[key] < self._cache_ttl:
            return cached.copy()
        return None

    def _validate(self, symbol: str, timeframe: str, frame: Any) -> pd.DataFrame:
        if not isinstance(frame, pd.DataFrame) or frame.empty:
            logger.warning("No broker candles available for %s %s", symbol, timeframe)
            return self.empty_frame()
        missing = set(REQUIRED_COLUMNS).difference(frame.columns)
        if missing:
            logger.error("Broker candle payload for %s %s is missing %s", symbol, timeframe, sorted(missing))
            return self.empty_frame()
        cleaned = frame.loc[:, list(REQUIRED_COLUMNS)].copy()
        cleaned = cleaned.dropna(subset=["time", "open", "high", "low", "close"])
        cleaned = cleaned.sort_values("time").drop_duplicates(subset=["time"], keep="last").reset_index(drop=True)
        return cleaned

    async def get_candles(self, symbol: str, timeframe: str, count: int = 200) -> pd.DataFrame:
        """Return only broker-native, closed OHLCV candles for an allowed symbol."""
        cache_key = f"{symbol}_{timeframe}_{count}"
        cached = self._cache_get(cache_key)
        if cached is not None:
            return cached
        if self.executor is None or not self._broker_available:
            logger.error("Skipping %s %s: broker data is not connected", symbol, timeframe)
            return self.empty_frame()
        getter = getattr(self.executor, "get_candles", None)
        if not callable(getter):
            logger.error("Skipping %s %s: executor has no closed-candle API", symbol, timeframe)
            return self.empty_frame()
        try:
            frame = self._validate(symbol, timeframe, await getter(symbol, timeframe, count))
        except Exception as exc:
            logger.error("Broker candle request failed for %s %s: %s", symbol, timeframe, exc)
            return self.empty_frame()
        if not frame.empty:
            self._cache[cache_key] = frame
            self._cache_time[cache_key] = datetime.utcnow()
        return frame.copy()

    async def get_historical(
        self, symbol: str, timeframe: str, start: datetime, end: datetime
    ) -> pd.DataFrame:
        """Return broker history or an empty frame; no external or synthetic fallback."""
        if self.executor is None or not self._broker_available:
            logger.error("Skipping historical request for %s: broker data is not connected", symbol)
            return self.empty_frame()
        getter = getattr(self.executor, "get_historical_candles", None)
        if not callable(getter):
            logger.error("Skipping historical request for %s: executor has no historical-candle API", symbol)
            return self.empty_frame()
        try:
            return self._validate(symbol, timeframe, await getter(symbol, timeframe, start, end))
        except Exception as exc:
            logger.error("Broker historical request failed for %s %s: %s", symbol, timeframe, exc)
            return self.empty_frame()

    async def get_price(self, symbol: str) -> tuple[float, float]:
        """Return broker bid/ask; no estimated spread or derived execution price."""
        if self.executor is None or not self._broker_available:
            return (0.0, 0.0)
        try:
            bid, ask = await self.executor.get_symbol_price(symbol)
            return (float(bid or 0.0), float(ask or 0.0))
        except Exception as exc:
            logger.error("Broker price request failed for %s: %s", symbol, exc)
            return (0.0, 0.0)
