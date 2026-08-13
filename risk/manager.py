"""Institutional risk, sizing, margin, exposure, and basket-layer controls.

Risk is calculated from broker symbol specifications and a structural stop. A
setup receives one finite risk budget; every layer consumes part of that same
budget. Scores, recent losses, available margin, and profit targets never
increase the configured maximum risk.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Optional

from config import TradeSettings
from executors.base import Position
from storage import db


@dataclass
class RiskCheckResult:
    passed: bool
    reason: str = ""
    checks: list[tuple[str, bool, str]] = field(default_factory=list)


@dataclass(frozen=True)
class PositionSizingResult:
    ideal_volume: float
    margin_limited_volume: float
    final_volume: float
    risk_amount: float
    expected_loss: float
    required_margin: float
    available_margin: float
    loss_per_lot: float
    reason: str = ""

    @property
    def valid(self) -> bool:
        return self.final_volume > 0 and not self.reason


@dataclass(frozen=True)
class LayerSpec:
    number: int
    allocation: float
    volume: float
    allocated_risk: float
    expected_loss: float
    trigger: str


@dataclass(frozen=True)
class BasketRiskSnapshot:
    maximum_risk: float
    current_risk: float
    remaining_risk: float
    exposure_pct: float


class RiskManager:
    """Evaluates hard financial constraints and creates non-martingale layer plans."""

    def __init__(self, settings: TradeSettings):
        self.settings = settings

    @staticmethod
    def _spec(symbol_info: dict, *names: str, default: float) -> float:
        for name in names:
            value = symbol_info.get(name)
            if value not in (None, 0, 0.0):
                return float(value)
        return float(default)

    def _risk_equity(self, account_equity: float) -> float:
        return float(self.settings.virtual_balance or account_equity)

    def floor_volume(self, volume: float, symbol_info: dict) -> float:
        """Round down to the broker's volume step without increasing risk."""
        min_volume = self._spec(symbol_info, "volume_min", "min_lot", default=0.01)
        max_volume = self._spec(symbol_info, "volume_max", "max_lot", default=100.0)
        step = self._spec(symbol_info, "volume_step", "step_lot", default=min_volume)
        if volume <= 0 or step <= 0:
            return 0.0

        floored = math.floor((min(volume, max_volume) + 1e-12) / step) * step
        # A volume below the broker minimum is invalid; never round it upward.
        if floored + 1e-12 < min_volume:
            return 0.0
        decimals = max(0, int(round(-math.log10(step))) + 2) if step < 1 else 2
        return round(floored, decimals)

    def normalize_lot(self, lot: float, symbol_info: dict) -> float:
        """Backward-compatible alias that preserves the safer floor-down behavior."""
        return self.floor_volume(lot, symbol_info)

    def loss_per_lot_at_stop(self, entry_price: float, stop_loss: float, symbol_info: dict) -> float:
        tick_size = self._spec(symbol_info, "trade_tick_size", "tick_size", default=0.0001)
        tick_value = self._spec(symbol_info, "trade_tick_value", "tick_value", default=1.0)
        distance = abs(float(entry_price) - float(stop_loss))
        if distance <= 0 or tick_size <= 0 or tick_value <= 0:
            return 0.0
        return (distance / tick_size) * tick_value

    def estimate_margin_per_lot(self, entry_price: float, symbol_info: dict, leverage: float) -> float:
        """Conservative MT5-specification margin estimate when order_calc_margin is unavailable."""
        contract_size = self._spec(symbol_info, "trade_contract_size", "contract_size", default=1.0)
        if entry_price <= 0 or leverage <= 0:
            return 0.0
        return abs(float(entry_price) * contract_size / float(leverage))

    def calculate_position_sizing(
        self,
        *,
        account_equity: float,
        free_margin: float,
        entry_price: float,
        stop_loss: float,
        symbol_info: dict,
        leverage: float,
        risk_pct: Optional[float] = None,
        margin_safety_buffer_pct: float = 0.10,
    ) -> PositionSizingResult:
        """Derive a valid final volume from risk and margin constraints.

        The smaller of risk-limited and margin-limited volume is chosen and
        rounded down. A lower margin capacity therefore reduces volume instead
        of unnecessarily rejecting a valid setup.
        """
        equity = self._risk_equity(account_equity)
        configured_risk_pct = self.settings.risk_per_trade if risk_pct is None else float(risk_pct)
        capped_risk_pct = min(max(configured_risk_pct, 0.0), self.settings.max_setup_risk_pct, 1.0)
        risk_amount = equity * capped_risk_pct / 100
        loss_per_lot = self.loss_per_lot_at_stop(entry_price, stop_loss, symbol_info)
        if risk_amount <= 0 or loss_per_lot <= 0:
            return PositionSizingResult(0, 0, 0, risk_amount, 0, 0, free_margin, loss_per_lot, "Invalid risk amount or structural stop")

        ideal_volume = risk_amount / loss_per_lot
        margin_per_lot = self.estimate_margin_per_lot(entry_price, symbol_info, leverage)
        available_margin = max(0.0, free_margin * (1 - max(0.0, margin_safety_buffer_pct)))
        margin_limited_volume = available_margin / margin_per_lot if margin_per_lot > 0 else ideal_volume
        final_volume = self.floor_volume(min(ideal_volume, margin_limited_volume), symbol_info)
        required_margin = final_volume * margin_per_lot
        expected_loss = final_volume * loss_per_lot

        if final_volume <= 0:
            reason = "No broker-valid volume fits the risk and margin constraints"
        elif required_margin > available_margin + 1e-9:
            reason = "Final volume exceeds available margin after safety buffer"
        elif expected_loss > risk_amount + 1e-6:
            reason = "Final volume exceeds the setup risk budget"
        else:
            reason = ""

        return PositionSizingResult(
            ideal_volume=ideal_volume,
            margin_limited_volume=margin_limited_volume,
            final_volume=final_volume,
            risk_amount=risk_amount,
            expected_loss=expected_loss,
            required_margin=required_margin,
            available_margin=available_margin,
            loss_per_lot=loss_per_lot,
            reason=reason,
        )

    def calculate_position_size(
        self,
        account_balance: float,
        entry_price: float,
        stop_loss: float,
        symbol_info: dict,
    ) -> float:
        """Compatibility helper for callers that have not yet supplied margin data."""
        loss_per_lot = self.loss_per_lot_at_stop(entry_price, stop_loss, symbol_info)
        risk_amount = self._risk_equity(account_balance) * min(self.settings.risk_per_trade, self.settings.max_setup_risk_pct, 1.0) / 100
        return self.floor_volume(risk_amount / loss_per_lot if loss_per_lot else 0.0, symbol_info)

    def calculate_position_risk(self, position: Position, symbol_info: dict) -> float:
        """Worst-case *loss* at the active SL; protected profit does not use loss budget."""
        if position.sl is None or position.sl <= 0:
            return float("inf")
        if position.direction == "BUY":
            loss_distance = max(0.0, position.entry_price - position.sl)
        else:
            loss_distance = max(0.0, position.sl - position.entry_price)
        if loss_distance <= 0:
            return 0.0
        tick_size = self._spec(symbol_info, "trade_tick_size", "tick_size", default=0.0001)
        tick_value = self._spec(symbol_info, "trade_tick_value", "tick_value", default=1.0)
        return (loss_distance / tick_size) * tick_value * float(position.volume)

    def basket_snapshot(
        self,
        *,
        account_equity: float,
        current_basket_risk: float,
        proposed_layer_risk: float = 0.0,
    ) -> BasketRiskSnapshot:
        maximum = self._risk_equity(account_equity) * min(self.settings.max_setup_risk_pct, 1.0) / 100
        current = max(0.0, current_basket_risk + proposed_layer_risk)
        remaining = max(0.0, maximum - current)
        exposure_pct = current / self._risk_equity(account_equity) * 100 if self._risk_equity(account_equity) else 0.0
        return BasketRiskSnapshot(maximum, current, remaining, exposure_pct)

    def get_layering_plan(
        self,
        total_lot: float,
        entry: float,
        sl: float,
        symbol_info: dict,
        *,
        account_equity: Optional[float] = None,
        allocation: Optional[list[float]] = None,
    ) -> list[dict]:
        """Return planned layer volumes within one fixed basket-risk budget.

        The plan is metadata only: callers execute layer one initially and may
        add later layers only after fresh structural revalidation. No layer is
        created to average a losing position.
        """
        raw_allocation = allocation or self.settings.layer_allocation
        normalized_allocation = [max(float(value), 0.0) for value in raw_allocation[: self.settings.max_layers]]
        total_allocation = sum(normalized_allocation)
        if total_lot <= 0 or total_allocation <= 0:
            return []
        normalized_allocation = [value / total_allocation for value in normalized_allocation]

        loss_per_lot = self.loss_per_lot_at_stop(entry, sl, symbol_info)
        layers: list[dict] = []
        allocated_volume = 0.0
        for index, share in enumerate(normalized_allocation, start=1):
            raw_volume = total_lot * share
            if index == len(normalized_allocation):
                raw_volume = max(0.0, total_lot - allocated_volume)
            volume = self.floor_volume(raw_volume, symbol_info)
            if volume <= 0:
                continue
            allocated_volume += volume
            triggers = {
                1: "Initial validated entry",
                2: "Fresh structural confirmation",
                3: "Validated continuation or retest",
                4: "Lower-timeframe continuation confirmation",
            }
            layers.append(
                {
                    "number": index,
                    "lot": volume,
                    "allocation": share,
                    "expected_loss": volume * loss_per_lot,
                    "comment": f"SMC L{index}/{len(normalized_allocation)}",
                    "trigger": triggers.get(index, "Fresh revalidation required"),
                }
            )
        return layers

    async def check_all(
        self,
        symbol: str,
        direction: str,
        score: float,
        rr_ratio: float,
        spread_pips: float,
        account_equity: float,
        free_margin: float,
        required_margin: float,
        today_pnl: float,
        today_trade_count: int,
        open_position_count: int,
        *,
        proposed_setup_risk: float = 0.0,
        current_open_risk: float = 0.0,
        safety_margin_buffer_pct: float = 0.10,
        setup_valid: bool = True,
        is_layer: bool = False,
        consecutive_losses: int = 0,
    ) -> RiskCheckResult:
        """Run immutable financial and operational gates before execution."""
        checks: list[tuple[str, bool, str]] = []
        equity = self._risk_equity(account_equity)

        checks.append(("Auto-trade enabled", self.settings.auto_trade and not self.settings.is_paused, f"auto_trade={self.settings.auto_trade}, paused={self.settings.is_paused}"))
        checks.append(("Setup validity", setup_valid, "Mandatory SMC sequence passed" if setup_valid else "Mandatory SMC validity failed"))
        checks.append(("Symbol allowed", symbol in self.settings.enabled_symbols, f"{symbol} enabled"))

        in_cooldown = await db.is_symbol_in_cooldown(symbol, self.settings.symbol_cooldown_minutes)
        checks.append(("Symbol not in cooldown", is_layer or not in_cooldown, f"cooldown={self.settings.symbol_cooldown_minutes} minutes" + (" (existing basket layer)" if is_layer else "")))

        daily_loss_amount = equity * self.settings.max_daily_loss_pct / 100
        emergency_loss_amount = equity * self.settings.absolute_daily_stop_pct / 100
        daily_loss_ok = today_pnl > -daily_loss_amount - 1e-6 and today_pnl > -emergency_loss_amount - 1e-6
        checks.append(("Daily loss limit", daily_loss_ok, f"PnL=${today_pnl:.2f}; normal stop=-${daily_loss_amount:.2f}; emergency=-${emergency_loss_amount:.2f}"))

        profit_stop_amount = equity * self.settings.daily_profit_stop_pct / 100
        profit_stop_ok = today_pnl < profit_stop_amount - 1e-6
        checks.append(("Daily profit stop", profit_stop_ok, f"PnL=${today_pnl:.2f}; stop=+${profit_stop_amount:.2f}"))

        checks.append(("Daily trade count", is_layer or today_trade_count < self.settings.max_trades_per_day, f"{today_trade_count}/{self.settings.max_trades_per_day}" + (" (existing basket layer)" if is_layer else "")))
        checks.append(("Loss-streak circuit breaker", consecutive_losses < self.settings.max_consecutive_losses, f"{consecutive_losses}/{self.settings.max_consecutive_losses} consecutive losses"))
        checks.append(("Max open positions", is_layer or open_position_count < self.settings.max_open_positions, f"{open_position_count}/{self.settings.max_open_positions}" + (" (existing basket layer)" if is_layer else "")))
        checks.append(("Setup quality", score >= self.settings.min_setup_score - 0.01, f"score={score:.1f}; minimum={self.settings.min_setup_score:.1f}"))
        checks.append(("Minimum RR", rr_ratio >= self.settings.min_rr_ratio - 0.001, f"RR=1:{rr_ratio:.2f}; minimum=1:{self.settings.min_rr_ratio:.2f}"))

        max_setup_risk_amount = equity * min(self.settings.max_setup_risk_pct, 1.0) / 100
        checks.append(("Setup risk budget", proposed_setup_risk <= max_setup_risk_amount + 1e-6, f"proposed=${proposed_setup_risk:.2f}; max=${max_setup_risk_amount:.2f}"))

        max_open_risk_amount = equity * self.settings.max_total_open_risk_pct / 100
        combined_open_risk = current_open_risk + proposed_setup_risk
        checks.append(("Total open-risk ceiling", combined_open_risk <= max_open_risk_amount + 1e-6, f"open+proposed=${combined_open_risk:.2f}; ceiling=${max_open_risk_amount:.2f}"))

        safe_free_margin = max(0.0, free_margin * (1 - safety_margin_buffer_pct))
        checks.append(("Free margin", required_margin <= safe_free_margin + 1e-6, f"required=${required_margin:.2f}; safe free=${safe_free_margin:.2f}"))

        all_passed = all(passed for _, passed, _ in checks)
        failed = [name for name, passed, _ in checks if not passed]
        return RiskCheckResult(all_passed, f"Failed: {', '.join(failed)}" if failed else "All hard risk checks passed", checks)


__all__ = [
    "BasketRiskSnapshot",
    "LayerSpec",
    "PositionSizingResult",
    "RiskCheckResult",
    "RiskManager",
]
