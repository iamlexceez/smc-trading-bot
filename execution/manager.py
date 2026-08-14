"""Policy-driven management for broker-synchronized protected positions.

This module preserves operational invariants: it never widens a stop, sends at
most one material action at a time, and uses initial risk for R calculations.
Management choices themselves are supplied by the selected experiment policy.
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
    """Recommend one management action using a supplied experimental policy."""

    def __init__(
        self,
        *,
        policy: Optional[dict] = None,
        breakeven_at_rr: Optional[float] = None,
        profit_lock_rr: Optional[float] = None,
        runner_rr: Optional[float] = None,
        min_rr: Optional[float] = None,
        stop_atr_buffer: Optional[float] = None,
        min_sl_update_distance: float = 0.0,
        min_tp_update_distance: float = 0.0,
        allow_partial_tp: Optional[bool] = None,
        allow_tp_extension: Optional[bool] = None,
    ):
        self.policy = dict(policy or {})
        self.breakeven_model = self.policy.get("breakeven_model", "rr")
        self.breakeven_at_rr = breakeven_at_rr if breakeven_at_rr is not None else self.policy.get("breakeven_trigger_r")
        self.profit_lock_rr = profit_lock_rr if profit_lock_rr is not None else self.policy.get("profit_lock_trigger_r")
        self.profit_lock_r = self.policy.get("profit_lock_r")
        self.trailing_model = self.policy.get("trailing_model", "structural")
        self.runner_rr = runner_rr if runner_rr is not None else self.policy.get("trailing_trigger_r")
        self.stop_atr_buffer = float(stop_atr_buffer if stop_atr_buffer is not None else (self.policy.get("trailing_buffer_atr") or 0.0))
        self.min_sl_update_distance = float(min_sl_update_distance)
        self.min_tp_update_distance = float(min_tp_update_distance)
        self.partial_exit_model = self.policy.get("partial_exit_model", "none")
        self.partial_exit_r = self.policy.get("partial_exit_r")
        self.partial_exit_pct = self.policy.get("partial_exit_pct")
        self.target_extension_trigger_r = self.policy.get("target_extension_trigger_r")
        self.target_model = self.policy.get("target_model", "liquidity")
        self.exit_on_opposing_structure = bool(self.policy.get("exit_on_opposing_structure", True))
        # This is a learnable policy response coefficient. It maps fresh capital
        # vulnerability into the timing of already policy-approved protective
        # actions; it never manufactures a stop/target or weakens integrity.
        self.protection_response = max(0.0, min(1.0, float(self.policy.get("protection_response", 0.5))))
        # Compatibility settings do not create a rule unless no explicit policy
        # object was supplied by a caller from a legacy path.
        self.allow_partial_tp = bool(allow_partial_tp) if allow_partial_tp is not None else self.partial_exit_model != "none"
        self.allow_tp_extension = bool(allow_tp_extension) if allow_tp_extension is not None else self.target_extension_trigger_r is not None
        self.min_rr = min_rr

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
        protection_context: Optional[dict] = None,
    ) -> TradeManagementAction:
        """Return at most one integrity-safe action selected by active policy."""
        snapshot = self.snapshot(
            direction=direction, entry_price=entry_price, initial_stop=initial_stop,
            current_price=current_price, state=state, structure=structure,
        )
        if snapshot.initial_risk <= 0:
            return TradeManagementAction("none", state, reason="Initial risk unavailable; broker modification withheld")
        event = structure.last_event.event_type
        structural_confirmation = _same_direction_event(direction, event)
        current_r = snapshot.current_r
        protection_score = max(0.0, min(1.0, float((protection_context or {}).get("score") or 0.0)))
        protection_adjustment = protection_score * self.protection_response
        protection_suffix = f" under capital protection {protection_score:.2f}" if protection_adjustment > 0 else ""

        if self.exit_on_opposing_structure and _opposing_event(direction, event):
            return TradeManagementAction("close_full", ManagementState.EXIT_WARNING, reason=f"Policy exits on opposing {event.value}")

        trailing_trigger = float(self.runner_rr) * (1.0 - 0.35 * protection_adjustment) if self.runner_rr is not None else None
        if self.trailing_model == "structural" and trailing_trigger is not None and current_r >= trailing_trigger and structural_confirmation:
            structural_sl = self._structural_trail(direction=direction, current_sl=current_sl, structure=structure, atr_value=atr_value)
            if structural_sl is not None:
                return TradeManagementAction("move_sl", ManagementState.RUNNER, new_sl=structural_sl, reason=f"Policy structural trail after {current_r:.2f}R{protection_suffix}")

        profit_lock_trigger = float(self.profit_lock_rr) * (1.0 - 0.35 * protection_adjustment) if self.profit_lock_rr is not None else None
        if profit_lock_trigger is not None and self.profit_lock_r is not None and current_r >= profit_lock_trigger and structural_confirmation:
            protected = entry_price + snapshot.initial_risk * float(self.profit_lock_r) if direction == "BUY" else entry_price - snapshot.initial_risk * float(self.profit_lock_r)
            if self._is_improvement(direction, current_sl, protected, self.min_sl_update_distance):
                return TradeManagementAction("move_sl", ManagementState.PROFIT_PROTECTED, new_sl=protected, reason=f"Policy profit lock at {self.profit_lock_r}R{protection_suffix}")

        breakeven_trigger = float(self.breakeven_at_rr) * (1.0 - 0.35 * protection_adjustment) if self.breakeven_at_rr is not None else None
        if self.breakeven_model != "none" and breakeven_trigger is not None and current_r >= breakeven_trigger:
            if self.breakeven_model != "structural" or structural_confirmation:
                break_even = entry_price + costs_buffer if direction == "BUY" else entry_price - costs_buffer
                if self._is_improvement(direction, current_sl, break_even, self.min_sl_update_distance):
                    return TradeManagementAction("move_sl", ManagementState.BE_ELIGIBLE, new_sl=break_even, reason=f"Policy breakeven at {current_r:.2f}R{protection_suffix}")

        partial_trigger = float(self.partial_exit_r) * (1.0 - 0.25 * protection_adjustment) if self.partial_exit_r is not None else None
        if self.allow_partial_tp and not partial_exit_done and partial_trigger is not None and current_r >= partial_trigger:
            percent = float(self.partial_exit_pct or 0.0)
            if 0 < percent < 1:
                return TradeManagementAction("close_partial", ManagementState.RUNNER, close_percent=percent, reason=f"Policy partial exit at {current_r:.2f}R{protection_suffix}")

        if self.allow_tp_extension and self.target_model in {"liquidity", "structure", "dynamic", "adaptive"} and structural_target is not None and self.target_extension_trigger_r is not None and current_r >= float(self.target_extension_trigger_r):
            farther = structural_target > current_tp if direction == "BUY" else structural_target < current_tp
            materially_different = abs(structural_target - current_tp) >= self.min_tp_update_distance
            if farther and materially_different:
                return TradeManagementAction("move_tp", ManagementState.TARGET_EXTENSION, new_tp=structural_target, reason="Policy structural target extension")

        return TradeManagementAction("none", state, reason="No selected policy management action")


__all__ = ["ManagementSnapshot", "ManagementState", "TradeManagementAction", "TradeManager"]
