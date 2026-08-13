"""Causal SMC setup validator.

The validator separates *validity* from *quality*: a setup must pass every
mandatory market-structure, liquidity, zone, reward, and safety prerequisite
before the scoring engine is allowed to rank it.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Optional

import pandas as pd

from analysis.confirmation import ConfirmationResult, ConfirmationType, get_confirmation
from analysis.displacement import DisplacementResult, detect_displacement
from analysis.indicators import atr
from analysis.liquidity import (
    LiquidityPool,
    LiquiditySweep,
    build_liquidity_pools,
    detect_latest_sweep,
    select_market_target,
)
from analysis.structure import FairValueGap, MarketStructure, OrderBlock, StructureEventType, Trend
from analysis.supply_demand import SupplyDemandZone, ZoneType


class EntryMode(str, Enum):
    CONFIRMED = "confirmed"
    AGGRESSIVE = "aggressive"
    EXTREME = "extreme"


@dataclass(frozen=True)
class ValidationCheck:
    name: str
    passed: bool
    detail: str


@dataclass(frozen=True)
class ValidatedZone:
    source: str  # order_block | fvg | supply_demand
    top: float
    bottom: float
    direction: str
    reference_index: int
    detail: str

    def contains(self, price: float) -> bool:
        return self.bottom <= price <= self.top


@dataclass
class SetupValidationResult:
    valid: bool
    direction: str
    entry_mode: EntryMode
    checks: list[ValidationCheck] = field(default_factory=list)
    entry_price: float = 0.0
    stop_loss: float = 0.0
    take_profit: float = 0.0
    rr_ratio: float = 0.0
    zone: Optional[ValidatedZone] = None
    liquidity_pools: list[LiquidityPool] = field(default_factory=list)
    sweep: Optional[LiquiditySweep] = None
    displacement: Optional[DisplacementResult] = None
    confirmation: Optional[ConfirmationResult] = None
    target_pool: Optional[LiquidityPool] = None

    @property
    def rejection_reason(self) -> str:
        failed = [check.name for check in self.checks if not check.passed]
        return f"Failed validity: {', '.join(failed)}" if failed else ""

    @property
    def passed_checks(self) -> bool:
        return all(check.passed for check in self.checks)


def _event_matches_direction(event_type: StructureEventType, direction: str) -> bool:
    if direction == "BUY":
        return event_type in (StructureEventType.BOS_BULLISH, StructureEventType.CHOCH_BULLISH)
    return event_type in (StructureEventType.BOS_BEARISH, StructureEventType.CHOCH_BEARISH)


def _htf_context_matches(htf_structures: list[MarketStructure], direction: str) -> tuple[bool, str]:
    if not htf_structures:
        return False, "No higher-timeframe structures available"

    desired_trend = Trend.BULLISH if direction == "BUY" else Trend.BEARISH
    opposite_trend = Trend.BEARISH if direction == "BUY" else Trend.BULLISH
    aligned = [structure for structure in htf_structures if structure.trend == desired_trend]
    reversal = [
        structure
        for structure in htf_structures
        if _event_matches_direction(structure.last_event.event_type, direction)
    ]
    opposing = [structure for structure in htf_structures if structure.trend == opposite_trend]

    if aligned:
        return True, f"{len(aligned)}/{len(htf_structures)} HTF trends align"
    if reversal and not opposing:
        return True, "Confirmed HTF reversal event without opposing HTF trend"
    return False, "No aligned HTF trend or confirmed non-conflicting reversal"


def _valid_zones_at_entry(
    structure: MarketStructure,
    zones: list[SupplyDemandZone],
    direction: str,
    entry_price: float,
) -> list[ValidatedZone]:
    expected_zone_type = ZoneType.DEMAND if direction == "BUY" else ZoneType.SUPPLY
    expected_direction = "bullish" if direction == "BUY" else "bearish"
    candidates: list[ValidatedZone] = []

    for order_block in structure.order_blocks:
        if order_block.direction != expected_direction or order_block.mitigated:
            continue
        zone = ValidatedZone(
            source="order_block",
            top=max(float(order_block.high), float(order_block.low)),
            bottom=min(float(order_block.high), float(order_block.low)),
            direction=direction,
            reference_index=order_block.index,
            detail=f"{expected_direction.title()} order block",
        )
        if zone.contains(entry_price):
            candidates.append(zone)

    for fvg in structure.fvgs:
        if fvg.direction != expected_direction or fvg.mitigated:
            continue
        zone = ValidatedZone(
            source="fvg",
            top=max(float(fvg.top), float(fvg.bottom)),
            bottom=min(float(fvg.top), float(fvg.bottom)),
            direction=direction,
            reference_index=fvg.index,
            detail=f"{expected_direction.title()} fair value gap",
        )
        if zone.contains(entry_price):
            candidates.append(zone)

    for sd_zone in zones:
        if sd_zone.zone_type != expected_zone_type or not sd_zone.fresh:
            continue
        zone = ValidatedZone(
            source="supply_demand",
            top=float(sd_zone.top),
            bottom=float(sd_zone.bottom),
            direction=direction,
            reference_index=sd_zone.base_index,
            detail=f"Fresh {sd_zone.zone_type.value} zone",
        )
        if zone.contains(entry_price):
            candidates.append(zone)

    priority = {"order_block": 0, "fvg": 1, "supply_demand": 2}
    return sorted(candidates, key=lambda item: (priority[item.source], item.top - item.bottom))


def _derive_structural_stop(
    direction: str,
    structure: MarketStructure,
    zone: ValidatedZone,
    sweep: LiquiditySweep,
    atr_value: float,
    atr_buffer: float,
) -> float:
    """Place the stop beyond objective invalidation, with only a small ATR buffer."""
    buffer = max(atr_value * atr_buffer, 0.0)
    if direction == "BUY":
        candidates = [sweep.pool.level, zone.bottom]
        if structure.protected_low > 0:
            candidates.append(structure.protected_low)
        return min(candidates) - buffer

    candidates = [sweep.pool.level, zone.top]
    if structure.protected_high > 0:
        candidates.append(structure.protected_high)
    return max(candidates) + buffer


class SetupValidator:
    """Validate the required SMC sequence before quality scoring or execution."""

    def __init__(
        self,
        min_rr: float = 3.0,
        min_sweep_penetration_atr: float = 0.05,
        displacement_body_ratio: float = 0.60,
        displacement_range_ratio: float = 1.20,
        stop_atr_buffer: float = 0.15,
        require_ltf_confirmation: bool = True,
    ):
        self.min_rr = float(min_rr)
        self.min_sweep_penetration_atr = float(min_sweep_penetration_atr)
        self.displacement_body_ratio = float(displacement_body_ratio)
        self.displacement_range_ratio = float(displacement_range_ratio)
        self.stop_atr_buffer = float(stop_atr_buffer)
        self.require_ltf_confirmation = bool(require_ltf_confirmation)

    def observe(
        self,
        *,
        symbol: str,
        direction: str,
        timeframe: str,
        df: pd.DataFrame,
        structure: MarketStructure,
        htf_structures: list[MarketStructure],
        zones: list[SupplyDemandZone],
        entry_mode: EntryMode = EntryMode.AGGRESSIVE,
        ltf_df: Optional[pd.DataFrame] = None,
        target_rr: Optional[float] = None,
        stop_model: str = "structural",
        target_model: str = "liquidity",
    ) -> SetupValidationResult:
        """Build a broker-valid candidate while retaining SMC evidence as features.

        Unlike :meth:`validate`, this research method does not turn absent SMC
        features (sweep, FVG/OB zone, displacement, BOS, or confirmation) into a
        universal rejection.  It still refuses malformed data, invalid stops, or
        invalid targets.  Policy selection later decides which observed features
        are required by the current DEMO experiment.
        """
        requested_direction = direction.upper()
        result = SetupValidationResult(valid=False, direction=requested_direction, entry_mode=entry_mode)
        if df.empty or len(df) < 30:
            result.checks.append(ValidationCheck("Market data", False, "Insufficient closed candles"))
            return result
        entry = float(df.iloc[-1]["close"])
        result.entry_price = entry
        if entry <= 0:
            result.checks.append(ValidationCheck("Market price", False, "Non-positive close"))
            return result

        htf_valid, htf_detail = _htf_context_matches(htf_structures, requested_direction)
        result.checks.append(ValidationCheck("HTF context", htf_valid, htf_detail))
        pools = build_liquidity_pools(df, structure.swing_highs, structure.swing_lows, timeframe)
        result.liquidity_pools = pools
        required_side = "sell-side" if requested_direction == "BUY" else "buy-side"
        has_liquidity = any(pool.side.value == required_side and not pool.swept for pool in pools)
        result.checks.append(ValidationCheck("Meaningful liquidity", has_liquidity, f"Looking for {required_side} liquidity"))
        sweep = detect_latest_sweep(df, pools, requested_direction, min_penetration_atr=self.min_sweep_penetration_atr)
        result.sweep = sweep
        result.checks.append(ValidationCheck("Liquidity sweep", sweep is not None, f"Sweep found" if sweep else "No closed-candle sweep found"))
        displacement = detect_displacement(
            df, requested_direction,
            body_ratio_min=self.displacement_body_ratio,
            range_ratio_min=self.displacement_range_ratio,
        )
        result.displacement = displacement
        displacement_after_sweep = bool(sweep and displacement.confirmed and displacement.index >= sweep.index)
        result.checks.append(ValidationCheck("Directional displacement", displacement_after_sweep, displacement.detail))
        event = structure.last_event
        structure_valid = _event_matches_direction(event.event_type, requested_direction)
        result.checks.append(ValidationCheck("BOS/CHOCH confirmation", structure_valid, event.event_type.value))
        candidate_zones = _valid_zones_at_entry(structure, zones, requested_direction, entry)
        selected_zone = candidate_zones[0] if candidate_zones else None
        result.zone = selected_zone
        result.checks.append(ValidationCheck("Retracement into valid zone", selected_zone is not None, selected_zone.detail if selected_zone else "No matching zone at entry"))
        confirmation_df = ltf_df if ltf_df is not None and not ltf_df.empty else df
        if selected_zone is not None:
            confirmation = get_confirmation(
                confirmation_df, requested_direction, zone_top=selected_zone.top, zone_bottom=selected_zone.bottom,
                require_retest=True, require_candle=True, require_displacement=False,
            )
        else:
            confirmation = ConfirmationResult(False, ConfirmationType.NONE, "No zone available for confirmation test")
        result.confirmation = confirmation
        result.checks.append(ValidationCheck("LTF confirmation", confirmation.confirmed, confirmation.detail))

        atr_value = float(atr(df, 14).iloc[-1])
        if atr_value <= 0 or atr_value != atr_value:
            atr_value = max(entry * 0.001, 1e-9)
        buffer = max(atr_value * self.stop_atr_buffer, 0.0)
        if stop_model == "atr":
            stop_loss = entry - buffer if requested_direction == "BUY" else entry + buffer
        elif stop_model == "zone" and selected_zone is not None:
            stop_loss = selected_zone.bottom - buffer if requested_direction == "BUY" else selected_zone.top + buffer
        elif selected_zone is not None and sweep is not None:
            stop_loss = _derive_structural_stop(requested_direction, structure, selected_zone, sweep, atr_value, self.stop_atr_buffer)
        elif requested_direction == "BUY":
            candidates = [float(df["low"].tail(20).min())]
            if structure.protected_low > 0:
                candidates.append(float(structure.protected_low))
            stop_loss = min(candidates) - buffer
        else:
            candidates = [float(df["high"].tail(20).max())]
            if structure.protected_high > 0:
                candidates.append(float(structure.protected_high))
            stop_loss = max(candidates) + buffer
        result.stop_loss = stop_loss
        stop_valid = (requested_direction == "BUY" and 0 < stop_loss < entry) or (requested_direction == "SELL" and stop_loss > entry)
        result.checks.append(ValidationCheck("Executable stop", stop_valid, f"SL {stop_loss:.5f}" if stop_valid else "Could not derive valid stop"))
        risk = abs(entry - stop_loss)
        target_pool = select_market_target(pools, requested_direction, entry)
        result.target_pool = target_pool
        liquidity_target_valid = target_pool is not None and ((requested_direction == "BUY" and target_pool.level > entry) or (requested_direction == "SELL" and target_pool.level < entry))
        if target_model in {"liquidity", "structure", "dynamic", "adaptive"} and liquidity_target_valid:
            take_profit = float(target_pool.level)
        elif risk > 0:
            rr = float(target_rr or 1.0)
            take_profit = entry + risk * rr if requested_direction == "BUY" else entry - risk * rr
        else:
            take_profit = 0.0
        result.take_profit = take_profit
        target_valid = (requested_direction == "BUY" and take_profit > entry) or (requested_direction == "SELL" and 0 < take_profit < entry)
        result.checks.append(ValidationCheck("Executable target", target_valid, f"TP {take_profit:.5f}" if target_valid else "Could not derive valid target"))
        result.rr_ratio = abs(take_profit - entry) / risk if risk > 0 and target_valid else 0.0
        # Only data and broker-order shape are mandatory in research candidate generation.
        result.valid = stop_valid and target_valid
        return result

    def validate(
        self,
        *,
        symbol: str,
        direction: str,
        timeframe: str,
        df: pd.DataFrame,
        structure: MarketStructure,
        htf_structures: list[MarketStructure],
        zones: list[SupplyDemandZone],
        entry_mode: EntryMode = EntryMode.CONFIRMED,
        ltf_df: Optional[pd.DataFrame] = None,
    ) -> SetupValidationResult:
        """Return a complete, auditable validity decision for the latest closed bar."""
        requested_direction = direction.upper()
        result = SetupValidationResult(valid=False, direction=requested_direction, entry_mode=entry_mode)
        if df.empty or len(df) < 30:
            result.checks.append(ValidationCheck("Market data", False, "Insufficient closed candles"))
            return result

        entry = float(df.iloc[-1]["close"])
        result.entry_price = entry
        htf_valid, htf_detail = _htf_context_matches(htf_structures, requested_direction)
        result.checks.append(ValidationCheck("HTF context", htf_valid, htf_detail))

        pools = build_liquidity_pools(df, structure.swing_highs, structure.swing_lows, timeframe)
        result.liquidity_pools = pools
        required_side = "sell-side" if requested_direction == "BUY" else "buy-side"
        has_liquidity = any(pool.side.value == required_side and not pool.swept for pool in pools)
        result.checks.append(ValidationCheck("Meaningful liquidity", has_liquidity, f"Looking for {required_side} liquidity"))

        sweep = detect_latest_sweep(
            df,
            pools,
            requested_direction,
            min_penetration_atr=self.min_sweep_penetration_atr,
        )
        result.sweep = sweep
        result.checks.append(
            ValidationCheck(
                "Liquidity sweep",
                sweep is not None,
                f"Swept {sweep.pool.side.value} at {sweep.pool.level:.5f}" if sweep else "No closed-candle sweep found",
            )
        )

        displacement = detect_displacement(
            df,
            requested_direction,
            body_ratio_min=self.displacement_body_ratio,
            range_ratio_min=self.displacement_range_ratio,
        )
        result.displacement = displacement
        displacement_after_sweep = bool(sweep and displacement.confirmed and displacement.index >= sweep.index)
        result.checks.append(
            ValidationCheck(
                "Directional displacement",
                displacement_after_sweep,
                displacement.detail if displacement.confirmed else "No qualifying directional displacement",
            )
        )

        event = structure.last_event
        structure_valid = _event_matches_direction(event.event_type, requested_direction)
        result.checks.append(
            ValidationCheck(
                "BOS/CHOCH confirmation",
                structure_valid,
                event.event_type.value,
            )
        )

        candidate_zones = _valid_zones_at_entry(structure, zones, requested_direction, entry)
        selected_zone = candidate_zones[0] if candidate_zones else None
        result.zone = selected_zone
        result.checks.append(
            ValidationCheck(
                "Retracement into valid zone",
                selected_zone is not None,
                selected_zone.detail if selected_zone else f"Entry {entry:.5f} is outside every valid directional zone",
            )
        )

        confirmation_df = ltf_df if ltf_df is not None and not ltf_df.empty else df
        confirmation_required = entry_mode == EntryMode.CONFIRMED and self.require_ltf_confirmation
        if selected_zone is not None:
            confirmation = get_confirmation(
                confirmation_df,
                requested_direction,
                zone_top=selected_zone.top,
                zone_bottom=selected_zone.bottom,
                require_retest=confirmation_required,
                require_candle=confirmation_required,
                require_displacement=False,
            )
        else:
            confirmation = ConfirmationResult(False, ConfirmationType.NONE, "No valid zone to confirm")
        result.confirmation = confirmation
        result.checks.append(
            ValidationCheck(
                "LTF confirmation",
                confirmation.confirmed if confirmation_required else True,
                confirmation.detail if confirmation_required else f"Not required for {entry_mode.value} entry",
            )
        )

        if selected_zone is not None and sweep is not None:
            atr_value = float(atr(df, 14).iloc[-1])
            stop_loss = _derive_structural_stop(
                requested_direction,
                structure,
                selected_zone,
                sweep,
                atr_value,
                self.stop_atr_buffer,
            )
            target_pool = select_market_target(pools, requested_direction, entry)
            result.target_pool = target_pool
            result.stop_loss = stop_loss
            result.take_profit = target_pool.level if target_pool else 0.0

            stop_valid = (requested_direction == "BUY" and stop_loss < entry) or (requested_direction == "SELL" and stop_loss > entry)
            result.checks.append(ValidationCheck("Structural stop", stop_valid, f"SL {stop_loss:.5f}" if stop_valid else "Invalid structural stop"))

            target_valid = (
                target_pool is not None
                and ((requested_direction == "BUY" and target_pool.level > entry) or (requested_direction == "SELL" and target_pool.level < entry))
            )
            result.checks.append(
                ValidationCheck(
                    "Market-derived target",
                    target_valid,
                    f"Target {target_pool.level:.5f} from {target_pool.kind.value}" if target_valid else "No opposing liquidity target",
                )
            )

            risk = abs(entry - stop_loss)
            reward = abs(result.take_profit - entry) if target_valid else 0.0
            result.rr_ratio = reward / risk if risk > 0 else 0.0
            result.checks.append(
                ValidationCheck(
                    "Minimum RR",
                    result.rr_ratio >= self.min_rr,
                    f"RR 1:{result.rr_ratio:.2f}; minimum 1:{self.min_rr:.2f}",
                )
            )
        else:
            result.checks.extend(
                [
                    ValidationCheck("Structural stop", False, "Cannot derive stop without zone and sweep"),
                    ValidationCheck("Market-derived target", False, "Cannot derive target without valid setup"),
                    ValidationCheck("Minimum RR", False, "RR unavailable"),
                ]
            )

        result.valid = result.passed_checks
        return result


__all__ = [
    "EntryMode",
    "SetupValidationResult",
    "SetupValidator",
    "ValidatedZone",
    "ValidationCheck",
]
