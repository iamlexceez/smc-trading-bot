"""
Trade management — actively manage open positions.

Features:
- Breakeven: move SL to entry price at 1R profit
- Trailing stop: trail SL behind price using ATR
- Partial close: close 50% at 1:2, trail the rest
- Time-based exit: close if trade is open too long without progress
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Optional

logger = logging.getLogger(__name__)


@dataclass
class TradeManagementAction:
    action: str  # "move_sl", "close_partial", "close_full", "none"
    new_sl: Optional[float] = None
    close_percent: Optional[float] = None
    reason: str = ""


class TradeManager:
    """Manages open trades: breakeven, trailing, partial closes."""

    def __init__(
        self,
        breakeven_at_rr: float = 1.0,
        trail_atrr_multiplier: float = 2.0,
        partial_close_rr: float = 2.0,
        partial_close_percent: float = 0.50,
        max_hold_bars: int = 100,
    ):
        self.breakeven_at_rr = breakeven_at_rr
        self.trail_atrr_multiplier = trail_atrr_multiplier
        self.partial_close_rr = partial_close_rr
        self.partial_close_percent = partial_close_percent
        self.max_hold_bars = max_hold_bars

    def evaluate(
        self,
        direction: str,
        entry_price: float,
        current_sl: float,
        current_tp: float,
        current_price: float,
        atr_value: float,
        bars_held: int,
        partial_closed: bool = False,
    ) -> TradeManagementAction:
        """
        Evaluate a position and return recommended action.

        Args:
            direction: "BUY" or "SELL"
            entry_price: Original entry price
            current_sl: Current stop loss
            current_tp: Take profit
            current_price: Current market price
            atr_value: Current ATR value
            bars_held: Number of bars since entry
            partial_closed: Whether partial close already happened
        """
        risk = abs(entry_price - current_sl) if current_sl else abs(entry_price - current_price)
        if risk <= 0:
            return TradeManagementAction(action="none", reason="Invalid: zero risk")

        # Calculate current R multiple
        if direction == "BUY":
            current_rr = (current_price - entry_price) / risk
        else:
            current_rr = (entry_price - current_price) / risk

        # 1. Time-based exit: if held too long without hitting 1R
        if bars_held > self.max_hold_bars and current_rr < self.breakeven_at_rr:
            return TradeManagementAction(
                action="close_full",
                reason=f"Time exit: {bars_held} bars held, RR={current_rr:.2f} (below {self.breakeven_at_rr}R)",
            )

        # 2. Breakeven: move SL to entry at breakeven_at_rr
        if current_rr >= self.breakeven_at_rr:
            if direction == "BUY" and current_sl < entry_price:
                return TradeManagementAction(
                    action="move_sl",
                    new_sl=entry_price,
                    reason=f"Breakeven: RR={current_rr:.2f}R, SL moved to entry",
                )
            elif direction == "SELL" and current_sl > entry_price:
                return TradeManagementAction(
                    action="move_sl",
                    new_sl=entry_price,
                    reason=f"Breakeven: RR={current_rr:.2f}R, SL moved to entry",
                )

        # 3. Partial close: close 50% at partial_close_rr
        if not partial_closed and current_rr >= self.partial_close_rr:
            return TradeManagementAction(
                action="close_partial",
                close_percent=self.partial_close_percent,
                reason=f"Partial close: {self.partial_close_percent*100:.0f}% at {current_rr:.2f}R",
            )

        # 4. Trailing stop: trail behind price using ATR
        if current_rr >= self.breakeven_at_rr:
            trail_distance = atr_value * self.trail_atrr_multiplier
            if direction == "BUY":
                new_sl = current_price - trail_distance
                # Only move SL forward, never backward
                if new_sl > current_sl:
                    return TradeManagementAction(
                        action="move_sl",
                        new_sl=new_sl,
                        reason=f"Trailing: SL={new_sl:.5f} (ATR trail {trail_distance:.5f})",
                    )
            else:
                new_sl = current_price + trail_distance
                if new_sl < current_sl or current_sl == 0:
                    return TradeManagementAction(
                        action="move_sl",
                        new_sl=new_sl,
                        reason=f"Trailing: SL={new_sl:.5f} (ATR trail {trail_distance:.5f})",
                    )

        return TradeManagementAction(action="none", reason="No action needed")
