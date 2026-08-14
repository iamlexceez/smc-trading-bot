"""Persistent, evidence-based market behavior profiles.

Profiles describe observable broker-market behavior and completed trade outcomes.
They do not invent historical win rates, simulate proxy entries, or override the
hard setup-validation gates.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Dict

import numpy as np
import pandas as pd

from storage import db
from analysis.opportunity import market_context

logger = logging.getLogger(__name__)


@dataclass
class SymbolProfile:
    symbol: str
    volatility_index: float
    structure_respect_score: float
    avg_fvg_fill_rate: float
    optimal_atr_multiplier: float
    historical_win_rate_ob: float
    historical_win_rate_fvg: float
    best_timeframe: str
    last_updated: pd.Timestamp
    sample_size: int = 0
    expectancy_r: float = 0.0
    regime: str = "UNKNOWN"
    atr_ratio: float = 1.0
    adx: float = 0.0
    rsi: float = 50.0
    momentum: float = 0.0


class AdaptiveProfiler:
    """Build and persist descriptive profiles from broker candles and outcomes."""

    def __init__(self) -> None:
        self.profiles: Dict[str, SymbolProfile] = {}

    @staticmethod
    def _atr(frame: pd.DataFrame, period: int = 14) -> pd.Series:
        high_low = frame["high"] - frame["low"]
        high_close = (frame["high"] - frame["close"].shift()).abs()
        low_close = (frame["low"] - frame["close"].shift()).abs()
        return pd.concat([high_low, high_close, low_close], axis=1).max(axis=1).rolling(period).mean()

    @staticmethod
    def _closed_candle_persistence(frame: pd.DataFrame) -> float:
        """Measure one-bar directional follow-through on already closed bars."""
        body = frame["close"] - frame["open"]
        next_move = frame["close"].shift(-1) - frame["close"]
        valid = (body != 0) & next_move.notna()
        if not valid.any():
            return 0.0
        follow = ((body[valid] * next_move[valid]) > 0).mean() * 100
        return float(follow)

    async def profile_symbol(
        self,
        symbol: str,
        df: pd.DataFrame,
        timeframe: str = "M15",
        account_mode: str = "demo",
    ) -> SymbolProfile:
        """Persist a profile derived only from known candles and completed trades."""
        if df.empty or len(df) < 30:
            return self._default_profile(symbol, timeframe)

        frame = df.copy().tail(500).reset_index(drop=True)
        context = market_context(frame)
        atr_series = self._atr(frame)
        atr_values = atr_series.dropna()
        if atr_values.empty or float(frame["close"].iloc[-1]) == 0:
            return self._default_profile(symbol, timeframe)

        median_atr = float(atr_values.tail(100).median())
        close = float(frame["close"].iloc[-1])
        atr_pct = abs(median_atr / close) * 100
        candle_ranges = (frame["high"] - frame["low"]).tail(100)
        median_range = float(candle_ranges.median()) if not candle_ranges.empty else median_atr
        persistence = self._closed_candle_persistence(frame.tail(200))
        outcomes = await db.get_symbol_setup_metrics(symbol, timeframe, account_mode)
        all_outcomes = outcomes["all"]
        ob_outcomes = outcomes["order_block"]
        fvg_outcomes = outcomes["fvg"]

        # Outcome-derived fields remain neutral until there is a meaningful
        # sample. Descriptive observations never masquerade as an edge.
        min_evidence = 20
        historical_ob = ob_outcomes["win_rate"] if ob_outcomes["sample_size"] >= min_evidence else 0.0
        historical_fvg = fvg_outcomes["win_rate"] if fvg_outcomes["sample_size"] >= min_evidence else 0.0
        structure_score = persistence if all_outcomes["sample_size"] >= min_evidence else 0.0
        fvg_fill_proxy = historical_fvg / 100 if historical_fvg else 0.0

        profile = SymbolProfile(
            symbol=symbol,
            volatility_index=round(min(100.0, atr_pct * 50.0), 2),
            structure_respect_score=round(structure_score, 2),
            avg_fvg_fill_rate=round(fvg_fill_proxy, 4),
            optimal_atr_multiplier=round(max(0.5, min(3.0, median_range / max(median_atr, 1e-12))), 2),
            historical_win_rate_ob=round(historical_ob, 2),
            historical_win_rate_fvg=round(historical_fvg, 2),
            best_timeframe=timeframe,
            last_updated=pd.Timestamp.now(tz="UTC"),
            sample_size=int(all_outcomes["sample_size"]),
            expectancy_r=round(float(all_outcomes["expectancy_r"]), 4),
            regime=str(context.get("regime") or "UNKNOWN"),
            atr_ratio=round(float(context.get("atr_ratio") or 1.0), 4),
            adx=round(float(context.get("adx") or 0.0), 4),
            rsi=round(float(context.get("rsi") or 50.0), 4),
            momentum=round(float(context.get("momentum") or 0.0), 4),
        )
        self.profiles[symbol] = profile
        await db.upsert_symbol_profile(
            account_mode=account_mode,
            symbol=symbol,
            timeframe=timeframe,
            metrics={
                "volatility_index": profile.volatility_index,
                "median_atr": median_atr,
                "atr_pct": atr_pct,
                "median_range": median_range,
                "closed_candle_persistence_pct": persistence,
                "outcome_sample_size": profile.sample_size,
                "expectancy_r": profile.expectancy_r,
                "order_block": ob_outcomes,
                "fvg": fvg_outcomes,
                "regime": profile.regime,
                "atr_ratio": profile.atr_ratio,
                "adx": profile.adx,
                "rsi": profile.rsi,
                "momentum": profile.momentum,
            },
        )
        logger.info(
            "Updated %s %s profile: observations=%s completed_outcomes=%s",
            symbol,
            timeframe,
            len(frame),
            profile.sample_size,
        )
        return profile

    @staticmethod
    def _default_profile(symbol: str, timeframe: str) -> SymbolProfile:
        """Return an explicitly neutral profile when evidence is insufficient."""
        return SymbolProfile(
            symbol=symbol,
            volatility_index=0.0,
            structure_respect_score=0.0,
            avg_fvg_fill_rate=0.0,
            optimal_atr_multiplier=1.0,
            historical_win_rate_ob=0.0,
            historical_win_rate_fvg=0.0,
            best_timeframe=timeframe,
            last_updated=pd.Timestamp.now(tz="UTC"),
            sample_size=0,
            expectancy_r=0.0,
            regime="UNKNOWN",
            atr_ratio=1.0,
            adx=0.0,
            rsi=50.0,
            momentum=0.0,
        )


profiler = AdaptiveProfiler()
