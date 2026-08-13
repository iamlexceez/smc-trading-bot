"""Structure-first autonomous trade-management state machine.

The manager recommends a single material action at a time. It never widens a
stop, never modifies a target merely because a trade is profitable, and bases
R-multiples on the *initial* structural risk rather than a subsequently moved
stop.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Optional

from analysis.structure import MarketStructure, StructureEventType


class ManagementState(str, Enum):
    INITIAL = "initial"
    CONFIRMED = "confirmed"
    BE_ELIGIBLE = "be_eligible"
    PROFIT_PROTECTED = "profit_protected"
    RUNNER = "runner"
    TARGET_EXTENSION = "target_extension"
    EXIT_WARNING = "exit_warning"
    CLOSED = "closed"


@dataclass(frozen=True)
class TradeManagementAction:
    action: str  # move_sl | move_tp | close_partial | close_full | none
    state: ManagementState = ManagementState.INITIAL
    new_sl: Optional[float] = None
    new_tp: Optional[float] = None
    close_percent: Optional[float] = None
    reason: str = ""


@dataclass(frozen=True)
class ManagementSnapshot:
    current_r: float
    initial_risk: float
    state: ManagementState
    thesis_valid: bool


def _same_direction_event(direction: str, event: StructureEventType) -> bool:
    return (
        (direction == "BUY" and event in (StructureEventType.BOS_BULLISH, StructureEventType.CHOCH_BULLISH))
        or (direction == "SELL" and event in (StructureEventType.BOS_BEARISH, StructureEventType.CHOCH_BEARISH))
    )


def _opposing_event(direction: str, event: StructureEventType) -> bool:
    return (
        (direction == "BUY" and event in (StructureEventType.BOS_BEARISH, StructureEventType.CHOCH_BEARISH))
        or (direction == "SELL" and event in (StructureEventType.BOS_BULLISH, StructureEventType.CHOCH_BULLISH))
    )


class TradeManager:
    """Recommend structural trade-management actions for one protected position."""

    def __init__(
        self,
        *,
        breakeven_at_rr: float = 1.0,
        profit_lock_rr: float = 1.5,
        runner_rr: float = 2.0,
        min_rr: float = 3.0,
        stop_atr_buffer: float = 0.15,
        min_sl_update_distance: float = 0.0,
        min_tp_update_distance: float = 0.0,
        allow_partial_tp: bool = True,
        allow_tp_extension: bool = True,
    ):
        self.breakeven_at_rr = float(breakeven_at_rr)
        self.profit_lock_rr = float(profit_lock_rr)
        self.runner_rr = float(runner_rr)
        self.min_rr = float(min_rr)
        self.stop_atr_buffer = float(stop_atr_buffer)
        self.min_sl_update_distance = float(min_sl_update_distance)
        self.min_tp_update_distance = float(min_tp_update_distance)
        self.allow_partial_tp = bool(allow_partial_tp)
        self.allow_tp_extension = bool(allow_tp_extension)

    @staticmethod
    def current_r(direction: str, entry_price: float, initial_stop: float, current_price: float) -> float:
        initial_risk = abs(entry_price - initial_stop)
        if initial_risk <= 0:
            return 0.0
        favorable_move = current_price - entry_price if direction == "BUY" else entry_price - current_price
        return favorable_move / initial_risk

    def snapshot(
        self,
        *,
        direction: str,
        entry_price: float,
        initial_stop: float,
        current_price: float,
        state: ManagementState,
        structure: Optional[MarketStructure],
    ) -> ManagementSnapshot:
        event = structure.last_event.event_type if structure else StructureEventType.NONE
        thesis_valid = not _opposing_event(direction, event)
        return ManagementSnapshot(
            current_r=self.current_r(direction, entry_price, initial_stop, current_price),
            initial_risk=abs(entry_price - initial_stop),
            state=state,
            thesis_valid=thesis_valid,
        )

    @staticmethod
    def _is_improvement(direction: str, current_sl: float, candidate_sl: float, minimum_distance: float) -> bool:
        if candidate_sl <= 0:
            return False
        if direction == "BUY":
            return candidate_sl > current_sl + minimum_distance
        return current_sl <= 0 or candidate_sl < current_sl - minimum_distance

    def _structural_trail(
        self,
        *,
        direction: str,
        current_sl: float,
        structure: MarketStructure,
        atr_value: float,
    ) -> Optional[float]:
        buffer = max(0.0, atr_value * self.stop_atr_buffer)
        if direction == "BUY" and structure.swing_lows:
            candidate = structure.swing_lows[-1].price - buffer
            return candidate if self._is_improvement(direction, current_sl, candidate, self.min_sl_update_distance) else None
        if direction == "SELL" and structure.swing_highs:
            candidate = structure.swing_highs[-1].price + buffer
            return candidate if self._is_improvement(direction, current_sl, candidate, self.min_sl_update_distance) else None
        return None

    def evaluate(
        self,
        *,
        direction: str,
        entry_price: float,
        initial_stop: float,
        current_sl: float,
        current_tp: float,
        current_price: float,
        atr_value: float,
        structure: MarketStructure,
        state: ManagementState = ManagementState.INITIAL,
        partial_exit_done: bool = False,
        structural_target: Optional[float] = None,
        costs_buffer: float = 0.0,
    ) -> TradeManagementAction:
        """Return at most one safe action from current, freshly analysed structure."""
        snapshot = self.snapshot(
            direction=direction,
            entry_price=entry_price,
            initial_stop=initial_stop,
            current_price=current_price,
            state=state,
            structure=structure,
        )
        if snapshot.initial_risk <= 0:
            return TradeManagementAction("none", state, reason="Initial structural risk unavailable")

        event = structure.last_event.event_type
        if _opposing_event(direction, event):
            return TradeManagementAction(
                "close_full",
                ManagementState.EXIT_WARNING,
                reason=f"Opposing {event.value} invalidates the active thesis",
            )

        structural_confirmation = _same_direction_event(direction, event)
        current_r = snapshot.current_r

        # A bona fide structural trail is always preferred over a fixed ATR trail.
        if current_r >= self.runner_rr and structural_confirmation:
            structural_sl = self._structural_trail(
                direction=direction,
                current_sl=current_sl,
                structure=structure,
                atr_value=atr_value,
            )
            if structural_sl is not None:
                return TradeManagementAction(
                    "move_sl",
                    ManagementState.RUNNER,
                    new_sl=structural_sl,
                    reason=f"{event.value} confirms a new protected structure level",
                )

        # At 2R, lock 1R only if this improves protection; never move backwards.
        if current_r >= self.runner_rr and structural_confirmation:
            lock_one_r = entry_price + snapshot.initial_risk if direction == "BUY" else entry_price - snapshot.initial_risk
            if self._is_improvement(direction, current_sl, lock_one_r, self.min_sl_update_distance):
                return TradeManagementAction(
                    "move_sl",
                    ManagementState.PROFIT_PROTECTED,
                    new_sl=lock_one_r,
                    reason=f"{current_r:.2f}R with directional structure confirmation; locking +1R",
                )

        # At roughly 1.5R, a structure-backed lock of half-R is permitted.
        if current_r >= self.profit_lock_rr and structural_confirmation:
            lock_half_r = entry_price + snapshot.initial_risk * 0.5 if direction == "BUY" else entry_price - snapshot.initial_risk * 0.5
            if self._is_improvement(direction, current_sl, lock_half_r, self.min_sl_update_distance):
                return TradeManagementAction(
                    "move_sl",
                    ManagementState.PROFIT_PROTECTED,
                    new_sl=lock_half_r,
                    reason=f"{current_r:.2f}R with directional structure confirmation; locking +0.5R",
                )

        # Breakeven needs both R progress and a fresh directional structural event.
        if current_r >= self.breakeven_at_rr and structural_confirmation:
            break_even = entry_price + costs_buffer if direction == "BUY" else entry_price - costs_buffer
            if self._is_improvement(direction, current_sl, break_even, self.min_sl_update_distance):
                return TradeManagementAction(
                    "move_sl",
                    ManagementState.BE_ELIGIBLE,
                    new_sl=break_even,
                    reason=f"{current_r:.2f}R and {event.value} permit protected breakeven",
                )

        # Partial exits are optional and happen only after core protection logic.
        if self.allow_partial_tp and not partial_exit_done and current_r >= self.runner_rr:
            return TradeManagementAction(
                "close_partial",
                ManagementState.RUNNER,
                close_percent=0.20,
                reason=f"Optional 20% realization at {current_r:.2f}R after protection review",
            )

        # A target may be extended only to a new, favourable structural target
        # that still offers at least the configured RR; it is never manufactured.
        if self.allow_tp_extension and structural_target is not None and current_r >= 2.5:
            risk = snapshot.initial_risk
            proposed_rr = abs(structural_target - entry_price) / risk if risk else 0.0
            farther = structural_target > current_tp if direction == "BUY" else structural_target < current_tp
            materially_different = abs(structural_target - current_tp) >= self.min_tp_update_distance
            if farther and materially_different and proposed_rr >= self.min_rr:
                return TradeManagementAction(
                    "move_tp",
                    ManagementState.TARGET_EXTENSION,
                    new_tp=structural_target,
                    reason=f"New external liquidity target preserves {proposed_rr:.2f}R",
                )

        return TradeManagementAction("none", state, reason="No material structure-backed management action")


__all__ = ["ManagementSnapshot", "ManagementState", "TradeManagementAction", "TradeManager"]
