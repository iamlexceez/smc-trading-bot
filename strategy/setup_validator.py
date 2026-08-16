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
from analysis.causality import is_available
from analysis.displacement import DisplacementResult, detect_displacement
from analysis.indicators import atr
from analysis.liquidity import (
    LiquidityPool,
    LiquiditySweep,
    build_liquidity_pools,
    detect_latest_sweep,
    select_market_targets,
)
from analysis.structure import MarketStructure, StructureEventType, Trend
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
    available_index: Optional[int] = None
    available_at: Optional[str] = None

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
    target_source: str = ""
    target_reason: str = ""
    target_candidates: list[dict] = field(default_factory=list)
    target_conflict: bool = False
    structural_target: float = 0.0
    structural_rr: float = 0.0
    minimum_rr: float = 0.0
    preferred_rr: float = 0.0
    rr_filter_enabled: bool = True
    rr_filter_status: str = "ENABLED"
    low_rr_experiment: bool = False

    @property
    def rejection_reason(self) -> str:
        failed = [check.name for check in self.checks if not check.passed]
        return f"Failed validity: {', '.join(failed)}" if failed else ""

    @property
    def passed_checks(self) -> bool:
        return all(check.passed for check in self.checks)


def calculate_rr(direction: str, entry_price: float, stop_loss: float, take_profit: float) -> tuple[float, float, float]:
    """Return risk distance, reward distance, and full-precision RR for valid directional geometry."""
    if str(direction).upper() == "BUY":
        risk_distance = float(entry_price) - float(stop_loss)
        reward_distance = float(take_profit) - float(entry_price)
    else:
        risk_distance = float(stop_loss) - float(entry_price)
        reward_distance = float(entry_price) - float(take_profit)
    if risk_distance <= 0 or reward_distance <= 0:
        return max(0.0, risk_distance), max(0.0, reward_distance), 0.0
    return risk_distance, reward_distance, reward_distance / risk_distance


def rr_filter_passes(actual_rr: float, configured_min_rr: float) -> bool:
    """Apply the explicit RR floor; zero means the RR-only filter is disabled."""
    if float(configured_min_rr or 0.0) <= 0.0:
        return True
    return float(actual_rr or 0.0) >= float(configured_min_rr)


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

    if aligned and opposing:
        return False, f"HTF context CONFLICTED: {len(aligned)} aligned and {len(opposing)} opposing timeframe bias(es)"
    if aligned and len(aligned) == len(htf_structures):
        return True, f"{len(aligned)}/{len(htf_structures)} HTF trends align"
    if reversal and not opposing:
        return True, "Confirmed HTF reversal event without opposing HTF trend"
    return False, "No aligned HTF trend or confirmed non-conflicting reversal"


def _valid_zones_at_entry(
    structure: MarketStructure,
    zones: list[SupplyDemandZone],
    direction: str,
    entry_price: float,
    decision_index: int,
) -> list[ValidatedZone]:
    expected_zone_type = ZoneType.DEMAND if direction == "BUY" else ZoneType.SUPPLY
    expected_direction = "bullish" if direction == "BUY" else "bearish"
    candidates: list[ValidatedZone] = []

    for order_block in structure.order_blocks:
        if not is_available(order_block, decision_index, fallback=order_block.index):
            continue
        if order_block.direction != expected_direction or order_block.mitigated:
            continue
        zone = ValidatedZone(
            source="order_block",
            top=max(float(order_block.high), float(order_block.low)),
            bottom=min(float(order_block.high), float(order_block.low)),
            direction=direction,
            reference_index=order_block.index,
            detail=f"{expected_direction.title()} order block",
            available_index=getattr(order_block, "available_index", order_block.index),
            available_at=getattr(order_block, "available_at", None),
        )
        if zone.contains(entry_price):
            candidates.append(zone)

    for fvg in structure.fvgs:
        if not is_available(fvg, decision_index, fallback=fvg.index):
            continue
        if fvg.direction != expected_direction or fvg.mitigated:
            continue
        zone = ValidatedZone(
            source="fvg",
            top=max(float(fvg.top), float(fvg.bottom)),
            bottom=min(float(fvg.top), float(fvg.bottom)),
            direction=direction,
            reference_index=fvg.index,
            detail=f"{expected_direction.title()} fair value gap",
            available_index=getattr(fvg, "available_index", fvg.index),
            available_at=getattr(fvg, "available_at", None),
        )
        if zone.contains(entry_price):
            candidates.append(zone)

    for sd_zone in zones:
        if not is_available(sd_zone, decision_index, fallback=sd_zone.base_index):
            continue
        if sd_zone.zone_type != expected_zone_type or not sd_zone.fresh:
            continue
        zone = ValidatedZone(
            source="supply_demand",
            top=float(sd_zone.top),
            bottom=float(sd_zone.bottom),
            direction=direction,
            reference_index=sd_zone.base_index,
            detail=f"Fresh {sd_zone.zone_type.value} zone",
            available_index=getattr(sd_zone, "available_index", sd_zone.base_index),
            available_at=getattr(sd_zone, "available_at", None),
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
        min_rr: float = 0.0,
        min_sweep_penetration_atr: float = 0.05,
        displacement_body_ratio: float = 0.60,
        displacement_range_ratio: float = 1.20,
        stop_atr_buffer: float = 0.15,
        require_ltf_confirmation: bool = True,
        rr_filter_enabled: bool = True,
        preferred_rr: float = 3.0,
        allow_low_rr_experiment: bool = False,
    ):
        self.min_rr = max(0.0, float(min_rr))
        self.rr_filter_enabled = bool(rr_filter_enabled) and self.min_rr > 0.0
        self.preferred_rr = max(0.0, float(preferred_rr))
        self.allow_low_rr_experiment = bool(allow_low_rr_experiment)
        self.min_sweep_penetration_atr = float(min_sweep_penetration_atr)
        self.displacement_body_ratio = float(displacement_body_ratio)
        self.displacement_range_ratio = float(displacement_range_ratio)
        self.stop_atr_buffer = float(stop_atr_buffer)
        self.require_ltf_confirmation = bool(require_ltf_confirmation)

    def _apply_rr_metadata(self, result: SetupValidationResult) -> None:
        result.minimum_rr = self.min_rr
        result.preferred_rr = self.preferred_rr
        result.rr_filter_enabled = self.rr_filter_enabled
        result.rr_filter_status = "ENABLED" if self.rr_filter_enabled else "DISABLED"
        result.low_rr_experiment = self.allow_low_rr_experiment

    def _finalize_target(
        self,
        result: SetupValidationResult,
        *,
        direction: str,
        entry: float,
        stop_loss: float,
        target_candidates: list[LiquidityPool],
        target_model: str,
        target_rr: Optional[float],
    ) -> None:
        """Select and validate a target without manufacturing reward."""
        risk = abs(float(entry) - float(stop_loss))
        result.target_candidates = [
            {
                "level": float(pool.level),
                "kind": pool.kind.value,
                "timeframe": pool.timeframe,
                "rr_ratio": abs(float(pool.level) - entry) / risk if risk > 0 else 0.0,
            }
            for pool in target_candidates
        ]
        target_pool = target_candidates[0] if target_candidates else None
        result.target_conflict = bool(
            target_candidates
            and target_model not in {"liquidity", "structure", "dynamic", "adaptive"}
            and target_rr is not None
        )
        if target_pool is not None:
            result.target_pool = target_pool
            result.structural_target = float(target_pool.level)
            result.structural_rr = abs(float(target_pool.level) - entry) / risk if risk > 0 else 0.0
            result.target_source = f"liquidity:{target_pool.kind.value}"
            result.target_reason = (
                f"Selected nearest opposing unswept {target_pool.kind.value} liquidity at {target_pool.level:.5f}; "
                f"structural RR 1:{result.structural_rr:.8f}"
            )
            take_profit = float(target_pool.level)
        elif target_model not in {"liquidity", "structure", "dynamic", "adaptive"} and risk > 0 and target_rr is not None and float(target_rr) > 0:
            result.target_source = "policy_rr_target"
            result.target_reason = f"Policy target model requested {float(target_rr):.8f}R; no legitimate structural target was available"
            take_profit = entry + risk * float(target_rr) if direction == "BUY" else entry - risk * float(target_rr)
        else:
            result.target_source = "UNKNOWN"
            result.target_reason = "No legitimate structural target or positive policy target was available"
            take_profit = 0.0
        result.take_profit = float(take_profit)
        target_valid = (direction == "BUY" and take_profit > entry) or (direction == "SELL" and 0 < take_profit < entry)
        result.checks.append(ValidationCheck("Executable target", target_valid, f"TP {take_profit:.5f}" if target_valid else "Could not derive valid target"))
        _, _, result.rr_ratio = calculate_rr(direction, entry, stop_loss, take_profit) if target_valid else (0.0, 0.0, 0.0)
        result.checks.append(ValidationCheck("Actual RR", target_valid, f"Actual RR {result.rr_ratio:.8f}; {result.target_reason}"))
        if not target_valid:
            result.valid = False
            return

        policy_target = result.target_source == "policy_rr_target"
        normal_floor = self.min_rr if self.rr_filter_enabled else 2.0
        rr_pass = rr_filter_passes(result.rr_ratio, self.min_rr) if self.rr_filter_enabled else True
        if policy_target and result.rr_ratio < normal_floor and not self.allow_low_rr_experiment:
            rr_pass = False
            rr_detail = (
                f"POLICY_TARGET_BELOW_NORMAL_RR: policy target produced 1:{result.rr_ratio:.8f}; "
                "an explicit LOW_RR_EXPERIMENT is required"
            )
        elif not rr_pass:
            rr_detail = f"STRUCTURAL_TARGET_BELOW_MIN_RR: structural target produced 1:{result.rr_ratio:.8f}; minimum is 1:{self.min_rr:.8f}"
        elif self.rr_filter_enabled:
            rr_detail = f"Actual RR 1:{result.rr_ratio:.8f} meets minimum 1:{self.min_rr:.8f}"
        else:
            rr_detail = "RR_FILTER_DISABLED: actual RR is recorded but does not independently reject this structural target"
        result.checks.append(ValidationCheck("Minimum RR", rr_pass, rr_detail))
        if not rr_pass:
            result.valid = False
        elif result.target_source == "policy_rr_target" and self.allow_low_rr_experiment and result.rr_ratio < normal_floor:
            result.rr_filter_status = "EXPERIMENTAL_LOW_RR"

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
        self._apply_rr_metadata(result)
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
        result.checks.append(ValidationCheck("Liquidity sweep", sweep is not None, "Sweep found" if sweep else "No closed-candle sweep found"))
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
        candidate_zones = _valid_zones_at_entry(
            structure, zones, requested_direction, entry, decision_index=len(df) - 1
        )
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
        if confirmation.confirmed and not is_available(
            confirmation, len(confirmation_df) - 1, fallback=confirmation.candle_index
        ):
            confirmation = ConfirmationResult(False, ConfirmationType.NONE, "Confirmation became available after the decision bar")
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
        target_candidates = select_market_targets(pools, requested_direction, entry)
        self._finalize_target(
            result,
            direction=requested_direction,
            entry=entry,
            stop_loss=stop_loss,
            target_candidates=target_candidates,
            target_model=target_model,
            target_rr=target_rr,
        )
        executable_checks = {"Executable stop", "Executable target", "Actual RR", "Minimum RR"}
        result.valid = stop_valid and all(
            check.passed for check in result.checks if check.name in executable_checks
        )
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
        self._apply_rr_metadata(result)
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

        candidate_zones = _valid_zones_at_entry(
            structure, zones, requested_direction, entry, decision_index=len(df) - 1
        )
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
        if confirmation.confirmed and not is_available(
            confirmation, len(confirmation_df) - 1, fallback=confirmation.candle_index
        ):
            confirmation = ConfirmationResult(False, ConfirmationType.NONE, "Confirmation became available after the decision bar")
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
            result.stop_loss = stop_loss
            stop_valid = (requested_direction == "BUY" and stop_loss < entry) or (requested_direction == "SELL" and stop_loss > entry)
            result.checks.append(ValidationCheck("Structural stop", stop_valid, f"SL {stop_loss:.5f}" if stop_valid else "Invalid structural stop"))
            target_candidates = select_market_targets(pools, requested_direction, entry)
            self._finalize_target(
                result,
                direction=requested_direction,
                entry=entry,
                stop_loss=stop_loss,
                target_candidates=target_candidates,
                target_model="liquidity",
                target_rr=None,
            )
        else:
            result.checks.extend(
                [
                    ValidationCheck("Structural stop", False, "Cannot derive stop without zone and sweep"),
                    ValidationCheck("Market-derived target", False, "Cannot derive target without valid setup"),
                    ValidationCheck("Actual RR", False, "RR unavailable because no structurally valid target exists"),
                    ValidationCheck("Minimum RR", False, "RR unavailable because no structurally valid target exists"),
                ]
            )

        result.valid = result.passed_checks
        return result


__all__ = [
    "calculate_rr",
    "rr_filter_passes",
    "EntryMode",
    "SetupValidationResult",
    "SetupValidator",
    "ValidatedZone",
    "ValidationCheck",
]
