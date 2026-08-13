"""Causal liquidity-pool and sweep detection for SMC execution.

Every pool is created from information confirmed on or before the current closed
candle.  A pool can only be swept by a later closed candle, preventing future
information from leaking into live signals or backtests.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Iterable, Optional

import pandas as pd

from analysis.indicators import atr
from analysis.structure import SwingPoint


class LiquiditySide(str, Enum):
    BUY_SIDE = "buy-side"
    SELL_SIDE = "sell-side"


class LiquidityKind(str, Enum):
    SWING = "swing"
    EQUAL_HIGH = "equal_high"
    EQUAL_LOW = "equal_low"
    SESSION_HIGH = "session_high"
    SESSION_LOW = "session_low"


@dataclass
class LiquidityPool:
    """A known liquidity reference available before any subsequent sweep."""

    level: float
    side: LiquiditySide
    kind: LiquidityKind
    timeframe: str
    created_index: int
    created_at: Optional[str] = None
    strength: float = 0.0
    swept: bool = False
    swept_index: Optional[int] = None
    swept_at: Optional[str] = None


@dataclass
class LiquiditySweep:
    """A candle-close-confirmed sweep of a previously known pool."""

    pool: LiquidityPool
    direction: str  # BUY after sell-side sweep, SELL after buy-side sweep
    index: int
    timestamp: Optional[str]
    penetration: float
    closed_back_inside: bool


def _timestamp(df: pd.DataFrame, index: int) -> Optional[str]:
    if "time" not in df.columns:
        return None
    return str(df.iloc[index]["time"])


def _dedupe_pools(pools: Iterable[LiquidityPool], tolerance: float) -> list[LiquidityPool]:
    """Keep the strongest pool when nearby levels describe the same liquidity."""
    result: list[LiquidityPool] = []
    for pool in sorted(pools, key=lambda item: (item.side.value, item.level, -item.strength)):
        existing = next(
            (
                candidate
                for candidate in result
                if candidate.side == pool.side and abs(candidate.level - pool.level) <= tolerance
            ),
            None,
        )
        if existing is None:
            result.append(pool)
        elif pool.strength > existing.strength:
            result[result.index(existing)] = pool
    return result


def build_liquidity_pools(
    df: pd.DataFrame,
    swing_highs: list[SwingPoint],
    swing_lows: list[SwingPoint],
    timeframe: str,
    equal_level_atr_tolerance: float = 0.15,
) -> list[LiquidityPool]:
    """Create causal swing and equal-high/equal-low liquidity pools.

    Swing highs are buy-side liquidity; swing lows are sell-side liquidity. A
    swing is assumed to be supplied only after its causal confirmation delay,
    which is already enforced by ``analysis.structure.detect_causal_swings``.
    """
    if df.empty:
        return []

    atr_value = float(atr(df, 14).iloc[-1]) if len(df) else 0.0
    tolerance = max(atr_value * equal_level_atr_tolerance, 1e-12)
    pools: list[LiquidityPool] = []

    for swing in swing_highs:
        pools.append(
            LiquidityPool(
                level=float(swing.price),
                side=LiquiditySide.BUY_SIDE,
                kind=LiquidityKind.SWING,
                timeframe=timeframe,
                created_index=int(swing.index),
                created_at=swing.timestamp,
                strength=60.0,
            )
        )

    for swing in swing_lows:
        pools.append(
            LiquidityPool(
                level=float(swing.price),
                side=LiquiditySide.SELL_SIDE,
                kind=LiquidityKind.SWING,
                timeframe=timeframe,
                created_index=int(swing.index),
                created_at=swing.timestamp,
                strength=60.0,
            )
        )

    for previous, current in zip(swing_highs, swing_highs[1:]):
        if abs(previous.price - current.price) <= tolerance:
            pools.append(
                LiquidityPool(
                    level=(float(previous.price) + float(current.price)) / 2,
                    side=LiquiditySide.BUY_SIDE,
                    kind=LiquidityKind.EQUAL_HIGH,
                    timeframe=timeframe,
                    created_index=int(current.index),
                    created_at=current.timestamp,
                    strength=90.0,
                )
            )

    for previous, current in zip(swing_lows, swing_lows[1:]):
        if abs(previous.price - current.price) <= tolerance:
            pools.append(
                LiquidityPool(
                    level=(float(previous.price) + float(current.price)) / 2,
                    side=LiquiditySide.SELL_SIDE,
                    kind=LiquidityKind.EQUAL_LOW,
                    timeframe=timeframe,
                    created_index=int(current.index),
                    created_at=current.timestamp,
                    strength=90.0,
                )
            )

    return _dedupe_pools(pools, tolerance)


def detect_latest_sweep(
    df: pd.DataFrame,
    pools: list[LiquidityPool],
    direction: str,
    min_penetration_atr: float = 0.05,
    lookback_bars: int = 6,
) -> Optional[LiquiditySweep]:
    """Return the latest legitimate sweep for the proposed trade direction.

    A SELL requires a buy-side pool to be breached by the candle high and then
    reclaimed by a close below the known pool. A BUY uses the symmetric sell-side
    condition. The sweep candle must be *after* the pool's creation index.
    """
    if df.empty or not pools:
        return None

    requested_direction = direction.upper()
    required_side = LiquiditySide.SELL_SIDE if requested_direction == "BUY" else LiquiditySide.BUY_SIDE
    current_atr = float(atr(df, 14).iloc[-1]) if len(df) else 0.0
    min_penetration = max(current_atr * min_penetration_atr, 1e-12)
    start_index = max(0, len(df) - lookback_bars)
    candidates: list[LiquiditySweep] = []

    for pool in pools:
        if pool.side != required_side or pool.swept:
            continue
        for candle_index in range(max(start_index, pool.created_index + 1), len(df)):
            candle = df.iloc[candle_index]
            if requested_direction == "BUY":
                penetration = pool.level - float(candle["low"])
                reclaimed = float(candle["close"]) > pool.level
            else:
                penetration = float(candle["high"]) - pool.level
                reclaimed = float(candle["close"]) < pool.level

            if penetration >= min_penetration and reclaimed:
                candidates.append(
                    LiquiditySweep(
                        pool=pool,
                        direction=requested_direction,
                        index=candle_index,
                        timestamp=_timestamp(df, candle_index),
                        penetration=penetration,
                        closed_back_inside=True,
                    )
                )

    if not candidates:
        return None

    latest = max(candidates, key=lambda item: item.index)
    latest.pool.swept = True
    latest.pool.swept_index = latest.index
    latest.pool.swept_at = latest.timestamp
    return latest


def select_market_targets(
    pools: list[LiquidityPool],
    direction: str,
    entry_price: float,
) -> list[LiquidityPool]:
    """Return legitimate opposing unswept liquidity targets nearest-first.

    The result is a market-structure hierarchy, not a manufactured RR ladder.
    Callers may evaluate farther structural targets only when nearer targets do
    not meet the active policy's RR requirement.
    """
    if direction.upper() == "BUY":
        candidates = [pool for pool in pools if pool.side == LiquiditySide.BUY_SIDE and not pool.swept and pool.level > entry_price]
        return sorted(candidates, key=lambda pool: pool.level)
    candidates = [pool for pool in pools if pool.side == LiquiditySide.SELL_SIDE and not pool.swept and pool.level < entry_price]
    return sorted(candidates, key=lambda pool: pool.level, reverse=True)


def select_market_target(
    pools: list[LiquidityPool],
    direction: str,
    entry_price: float,
) -> Optional[LiquidityPool]:
    """Return the nearest target for compatibility with existing callers."""
    candidates = select_market_targets(pools, direction, entry_price)
    return candidates[0] if candidates else None


__all__ = [
    "LiquidityKind",
    "LiquidityPool",
    "LiquiditySide",
    "LiquiditySweep",
    "build_liquidity_pools",
    "detect_latest_sweep",
    "select_market_target",
    "select_market_targets",
]
