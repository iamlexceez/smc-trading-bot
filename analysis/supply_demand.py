"""
Supply and Demand Zone Detection.

Detects:
- Supply zones (Rally-Base-Drop, Drop-Base-Drop)
- Demand zones (Drop-Base-Rally, Rally-Base-Rally)
- Zone freshness (unmitigated = fresh)
- Zone strength scoring
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
import pandas as pd
import numpy as np

from analysis.indicators import atr


class ZoneType(Enum):
    SUPPLY = "supply"
    DEMAND = "demand"


class ZonePattern(Enum):
    RALLY_BASE_DROP = "rally_base_drop"  # Supply
    DROP_BASE_DROP = "drop_base_drop"    # Supply
    DROP_BASE_RALLY = "drop_base_rally"  # Demand
    RALLY_BASE_RALLY = "rally_base_rally" # Demand


@dataclass
class SupplyDemandZone:
    top: float
    bottom: float
    zone_type: ZoneType
    pattern: ZonePattern
    base_index: int
    fresh: bool = True
    touches: int = 0
    strength: float = 0.0  # 0-100

    @property
    def midpoint(self) -> float:
        return (self.top + self.bottom) / 2

    @property
    def width(self) -> float:
        return abs(self.top - self.bottom)

    def contains_price(self, price: float) -> bool:
        return self.bottom <= price <= self.top


def detect_sd_zones(df: pd.DataFrame, lookback: int = 50, min_base_bars: int = 1, max_base_bars: int = 3) -> list[SupplyDemandZone]:
    """
    Detect supply and demand zones using base candle identification.

    A base is a consolidation (small bodies, overlapping ranges) followed by
    a strong impulse move away. The base defines the zone.

    Supply: base followed by strong drop
    Demand: base followed by strong rally
    """
    zones = []
    atr_val = atr(df, 14)

    if len(df) < 5:
        return zones

    start = max(3, len(df) - lookback)

    i = start
    while i < len(df) - 2:
        atr_i = atr_val.iloc[i] if not np.isnan(atr_val.iloc[i]) else 0.0001
        candle = df.iloc[i]
        body = abs(candle["close"] - candle["open"])

        # Check if this is a base candle (small body relative to ATR)
        if body < atr_i * 0.6:
            base_start = i
            base_end = i

            # Extend base while candles remain small
            j = i + 1
            while j < len(df) - 1 and j - i <= max_base_bars:
                next_candle = df.iloc[j]
                next_body = abs(next_candle["close"] - next_candle["open"])
                if next_body < atr_i * 0.6:
                    base_end = j
                    j += 1
                else:
                    break

            base_len = base_end - base_start + 1

            if base_len >= min_base_bars and j < len(df):
                impulse = df.iloc[j]
                impulse_body = abs(impulse["close"] - impulse["open"])

                # Strong impulse check
                if impulse_body > atr_i * 1.3:
                    base_high = max(df.iloc[k]["high"] for k in range(base_start, base_end + 1))
                    base_low = min(df.iloc[k]["low"] for k in range(base_start, base_end + 1))

                    # Determine direction
                    is_bullish_impulse = impulse["close"] > impulse["open"]

                    if is_bullish_impulse:
                        # Demand zone
                        # Pre-base direction determines pattern
                        pre_base = df.iloc[base_start - 1] if base_start > 0 else None
                        if pre_base is not None and pre_base["close"] < pre_base["open"]:
                            pattern = ZonePattern.DROP_BASE_RALLY
                        else:
                            pattern = ZonePattern.RALLY_BASE_RALLY

                        zone = SupplyDemandZone(
                            top=base_high,
                            bottom=base_low,
                            zone_type=ZoneType.DEMAND,
                            pattern=pattern,
                            base_index=base_start,
                        )
                    else:
                        # Supply zone
                        pre_base = df.iloc[base_start - 1] if base_start > 0 else None
                        if pre_base is not None and pre_base["close"] > pre_base["open"]:
                            pattern = ZonePattern.RALLY_BASE_DROP
                        else:
                            pattern = ZonePattern.DROP_BASE_DROP

                        zone = SupplyDemandZone(
                            top=base_high,
                            bottom=base_low,
                            zone_type=ZoneType.SUPPLY,
                            pattern=pattern,
                            base_index=base_start,
                        )

                    # Check freshness: has price returned to the zone since creation?
                    post_zone_high = df.iloc[j:]["high"].max() if j < len(df) else 0
                    post_zone_low = df.iloc[j:]["low"].min() if j < len(df) else 0

                    if zone.zone_type == ZoneType.SUPPLY:
                        zone.fresh = post_zone_low < zone.bottom  # Price hasn't returned up to zone
                        zone.touches = sum(
                            1 for k in range(j, len(df))
                            if df.iloc[k]["high"] >= zone.bottom
                        )
                    else:
                        zone.fresh = post_zone_high > zone.top  # Price hasn't returned down to zone
                        zone.touches = sum(
                            1 for k in range(j, len(df))
                            if df.iloc[k]["low"] <= zone.top
                        )

                    # Strength scoring: impulse strength, freshness, base tightness
                    impulse_strength = min(impulse_body / (atr_i * 2), 1.0) * 40
                    freshness_score = 40 if zone.fresh else (20 if zone.touches == 1 else 10)
                    base_tightness = (1 - min(base_len / max_base_bars, 1.0)) * 20
                    zone.strength = impulse_strength + freshness_score + base_tightness

                    zones.append(zone)
                    i = j + 1
                    continue

        i += 1

    # Sort by strength, return top zones
    zones.sort(key=lambda z: z.strength, reverse=True)
    return zones[:10]  # Top 10 zones


def get_nearest_zones(zones: list[SupplyDemandZone], current_price: float) -> dict:
    """Get nearest supply and demand zones to current price."""
    supply_zones = [z for z in zones if z.zone_type == ZoneType.SUPPLY]
    demand_zones = [z for z in zones if z.zone_type == ZoneType.DEMAND]

    nearest_supply = min(supply_zones, key=lambda z: abs(z.midpoint - current_price)) if supply_zones else None
    nearest_demand = min(demand_zones, key=lambda z: abs(z.midpoint - current_price)) if demand_zones else None

    return {
        "nearest_supply": nearest_supply,
        "nearest_demand": nearest_demand,
    }
