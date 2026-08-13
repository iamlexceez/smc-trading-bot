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
    # Explicit instrument sizing evidence. ``policy_required_lot`` is the raw
    # lot implied by the active policy; ``required_lot`` is the broker-step
    # normalized executable request when one exists.
    policy_required_lot: float = 0.0
    required_lot: float = 0.0
    broker_min_lot: float = 0.0
    broker_max_lot: float = 0.0
    broker_volume_step: float = 0.0
    margin_per_lot: float = 0.0
    minimum_lot_margin: float = 0.0
    minimum_lot_loss: float = 0.0
    sizing_code: str = ""

    @property
    def valid(self) -> bool:
        return self.final_volume > 0 and not self.reason

    def evidence(self) -> dict:
        """JSON-safe per-instrument lot calculation evidence for execution audits."""
        return {
            "policy_required_lot": self.policy_required_lot,
            "required_lot": self.required_lot,
            "final_volume": self.final_volume,
            "broker_min_lot": self.broker_min_lot,
            "broker_max_lot": self.broker_max_lot,
            "broker_volume_step": self.broker_volume_step,
            "margin_per_lot": self.margin_per_lot,
            "minimum_lot_margin": self.minimum_lot_margin,
            "minimum_lot_loss": self.minimum_lot_loss,
            "risk_amount": self.risk_amount,
            "expected_loss": self.expected_loss,
            "required_margin": self.required_margin,
            "available_margin": self.available_margin,
            "loss_per_lot": self.loss_per_lot,
            "sizing_code": self.sizing_code,
            "reason": self.reason,
        }


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

    def margin_per_lot(self, entry_price: float, symbol_info: dict, leverage: float) -> float:
        """Use fresh broker-native margin evidence first; estimate only as a compatibility fallback."""
        broker_value = self._spec(symbol_info, "margin_per_lot", "broker_margin_per_lot", default=0.0)
        if broker_value > 0:
            return broker_value
        minimum_volume = self._spec(symbol_info, "normalized_volume", "volume_min", "min_lot", default=0.0)
        minimum_margin = self._spec(symbol_info, "margin_required_min_volume", "margin_required", default=0.0)
        if minimum_volume > 0 and minimum_margin > 0:
            return minimum_margin / minimum_volume
        initial_margin = self._spec(symbol_info, "margin_initial", "initial_margin", default=0.0)
        if initial_margin > 0:
            return initial_margin
        contract_size = self._spec(symbol_info, "trade_contract_size", "contract_size", default=1.0)
        if entry_price <= 0 or leverage <= 0:
            return 0.0
        return abs(float(entry_price) * contract_size / float(leverage))

    # Backward-compatible name retained for isolated older callers.
    def estimate_margin_per_lot(self, entry_price: float, symbol_info: dict, leverage: float) -> float:
        return self.margin_per_lot(entry_price, symbol_info, leverage)

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
        risk_model: str = "fixed_pct",
        fixed_volume: Optional[float] = None,
        margin_safety_buffer_pct: float = 0.0,
    ) -> PositionSizingResult:
        """Derive broker-valid volume from policy-selected risk and free margin.

        The smaller of policy-risk and broker-margin capacity is chosen and
        rounded down. Broker volume steps and margin are integrity constraints;
        risk percentage is supplied by the active experiment.
        """
        equity = self._risk_equity(account_equity)
        configured_risk_pct = self.settings.risk_per_trade if risk_pct is None else float(risk_pct)
        broker_min_lot = self._spec(symbol_info, "volume_min", "min_lot", default=0.0)
        broker_max_lot = self._spec(symbol_info, "volume_max", "max_lot", default=0.0)
        broker_volume_step = self._spec(symbol_info, "volume_step", "step_lot", default=broker_min_lot)
        loss_per_lot = self.loss_per_lot_at_stop(entry_price, stop_loss, symbol_info)
        if loss_per_lot <= 0:
            return PositionSizingResult(0, 0, 0, 0, 0, 0, free_margin, loss_per_lot, "Invalid broker stop-loss specification", broker_min_lot=broker_min_lot, broker_max_lot=broker_max_lot, broker_volume_step=broker_volume_step, sizing_code="INVALID_STOP_SPEC")
        if risk_model == "fixed_volume":
            ideal_volume = max(0.0, float(fixed_volume or 0.0))
            risk_amount = ideal_volume * loss_per_lot
        else:
            # Experimental policy discovery: risk percentage is supplied by the
            # active policy rather than a hard predetermined ceiling.
            risk_amount = equity * max(0.0, configured_risk_pct) / 100
            ideal_volume = risk_amount / loss_per_lot if risk_amount > 0 else 0.0
        if ideal_volume <= 0:
            return PositionSizingResult(0, 0, 0, risk_amount, 0, 0, free_margin, loss_per_lot, "Policy did not specify a positive executable size", policy_required_lot=ideal_volume, broker_min_lot=broker_min_lot, broker_max_lot=broker_max_lot, broker_volume_step=broker_volume_step, sizing_code="POLICY_NO_POSITIVE_SIZE")

        margin_per_lot = self.margin_per_lot(entry_price, symbol_info, leverage)
        available_margin = max(0.0, free_margin * (1 - max(0.0, margin_safety_buffer_pct)))
        margin_limited_volume = available_margin / margin_per_lot if margin_per_lot > 0 else ideal_volume
        policy_required_lot = ideal_volume
        capped_required_lot = min(policy_required_lot, margin_limited_volume)
        required_lot = self.floor_volume(capped_required_lot, symbol_info)
        required_margin = required_lot * margin_per_lot
        expected_loss = required_lot * loss_per_lot
        minimum_lot_margin = broker_min_lot * margin_per_lot if broker_min_lot > 0 else 0.0
        minimum_lot_loss = broker_min_lot * loss_per_lot if broker_min_lot > 0 else 0.0

        sizing_code = ""
        if broker_min_lot <= 0 or broker_volume_step <= 0:
            sizing_code = "BROKER_VOLUME_SPEC_INVALID"
            reason = "Broker did not expose a positive minimum volume and volume step"
        elif margin_per_lot <= 0:
            sizing_code = "BROKER_MARGIN_UNAVAILABLE"
            reason = "No positive broker margin-per-lot evidence is available for this instrument"
        elif margin_limited_volume + 1e-12 < broker_min_lot:
            sizing_code = "MINIMUM_LOT_MARGIN_UNAFFORDABLE"
            reason = (f"Broker minimum lot {broker_min_lot:g} requires margin {minimum_lot_margin:.2f}, "
                      f"above available margin {available_margin:.2f}")
        elif risk_model != "fixed_volume" and policy_required_lot + 1e-12 < broker_min_lot:
            sizing_code = "MINIMUM_LOT_EXCEEDS_POLICY_RISK"
            reason = (f"Policy-required lot {policy_required_lot:.8g} is below broker minimum {broker_min_lot:g}; "
                      f"minimum lot loss {minimum_lot_loss:.2f} exceeds policy risk budget {risk_amount:.2f}")
        elif required_lot <= 0:
            sizing_code = "NO_STEP_NORMALIZED_LOT"
            reason = (f"Required lot {capped_required_lot:.8g} cannot be rounded down to broker step "
                      f"{broker_volume_step:g} at minimum {broker_min_lot:g}")
        elif required_margin > available_margin + 1e-9:
            sizing_code = "FINAL_LOT_MARGIN_EXCEEDS_AVAILABLE"
            reason = "Broker-normalized required lot exceeds available margin after safety buffer"
        elif risk_model != "fixed_volume" and expected_loss > risk_amount + 1e-6:
            sizing_code = "FINAL_LOT_EXCEEDS_POLICY_RISK"
            reason = "Broker-normalized required lot exceeds the selected policy risk budget"
        else:
            reason = ""

        return PositionSizingResult(
            ideal_volume=ideal_volume,
            margin_limited_volume=margin_limited_volume,
            final_volume=required_lot,
            risk_amount=risk_amount,
            expected_loss=expected_loss,
            required_margin=required_margin,
            available_margin=available_margin,
            loss_per_lot=loss_per_lot,
            reason=reason,
            policy_required_lot=policy_required_lot,
            required_lot=required_lot,
            broker_min_lot=broker_min_lot,
            broker_max_lot=broker_max_lot,
            broker_volume_step=broker_volume_step,
            margin_per_lot=margin_per_lot,
            minimum_lot_margin=minimum_lot_margin,
            minimum_lot_loss=minimum_lot_loss,
            sizing_code=sizing_code,
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
        risk_amount = self._risk_equity(account_balance) * max(0.0, self.settings.risk_per_trade) / 100
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
        maximum = self._risk_equity(account_equity) * max(0.0, self.settings.max_setup_risk_pct) / 100
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
        max_layers: Optional[int] = None,
        layer_style: str = "confirmation",
    ) -> list[dict]:
        """Return a policy-selected layer plan within the one basket-risk budget.

        Layering remains broker-volume-valid and later layers require fresh
        revalidation. The selected experiment, rather than a hidden global
        maximum, decides whether and how many layers it tests.
        """
        raw_allocation = allocation or self.settings.layer_allocation
        selected_max_layers = max_layers if max_layers is not None else self.settings.max_layers
        normalized_allocation = [max(float(value), 0.0) for value in raw_allocation[: max(0, int(selected_max_layers) + 1)]]
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
                1: "Initial policy entry",
                2: f"Policy layer: {layer_style}",
                3: f"Policy layer: {layer_style}",
                4: f"Policy layer: {layer_style}",
            }
            layers.append(
                {
                    "number": index,
                    "lot": volume,
                    "allocation": share,
                    "expected_loss": volume * loss_per_lot,
                    "comment": f"EXP L{index}/{len(normalized_allocation)}",
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
        safety_margin_buffer_pct: float = 0.0,
        setup_valid: bool = True,
        is_layer: bool = False,
        consecutive_losses: int = 0,
        policy: Optional[dict] = None,
    ) -> RiskCheckResult:
        """Run integrity checks plus the *selected* policy—never hidden global caps.

        The input policy is immutable experiment data.  Omitting a policy limit
        deliberately means that policy does not impose that type of trading
        restriction; it does not disable broker validation or emergency pause.
        """
        checks: list[tuple[str, bool, str]] = []
        policy = dict(policy or {})
        equity = self._risk_equity(account_equity)

        # Software/infrastructure integrity checks.
        checks.append(("Auto-trade enabled", self.settings.auto_trade and not self.settings.is_paused, f"auto_trade={self.settings.auto_trade}, paused={self.settings.is_paused}"))
        checks.append(("Executable market data", setup_valid, "Valid closed-candle price, stop, and target" if setup_valid else "Invalid candidate geometry"))
        checks.append(("Broker-verified enabled symbol", symbol in self.settings.enabled_symbols, f"{symbol} enabled"))
        safe_free_margin = max(0.0, free_margin * (1 - max(0.0, safety_margin_buffer_pct)))
        checks.append(("Free margin", required_margin <= safe_free_margin + 1e-6, f"required=${required_margin:.2f}; available=${safe_free_margin:.2f}"))

        # Experimental policy choices. These checks are applied only when the
        # active policy explicitly elects to use them.
        cooldown_minutes = policy.get("symbol_cooldown_minutes")
        if cooldown_minutes is not None:
            in_cooldown = await db.is_symbol_in_cooldown(symbol, int(cooldown_minutes))
            checks.append(("Policy symbol cooldown", is_layer or not in_cooldown, f"cooldown={cooldown_minutes} minutes"))
        daily_stop = policy.get("daily_stop_pct") if policy.get("daily_stop_model") != "none" else None
        if daily_stop is not None:
            checks.append(("Policy daily drawdown response", today_pnl > -(equity * float(daily_stop) / 100) - 1e-6, f"PnL=${today_pnl:.2f}; policy threshold=-{daily_stop}%"))
        daily_target = policy.get("daily_target_pct") if policy.get("daily_target_model") != "none" else None
        if daily_target is not None:
            checks.append(("Policy daily profit response", today_pnl < equity * float(daily_target) / 100 - 1e-6, f"PnL=${today_pnl:.2f}; policy target=+{daily_target}%"))
        max_trades = policy.get("max_trades_per_day")
        if max_trades is not None:
            checks.append(("Policy trade frequency", is_layer or today_trade_count < int(max_trades), f"{today_trade_count}/{max_trades}"))
        max_positions = policy.get("max_positions")
        if max_positions is not None:
            checks.append(("Policy concurrent positions", is_layer or open_position_count < int(max_positions), f"{open_position_count}/{max_positions}"))
        policy_risk_pct = policy.get("risk_pct")
        if policy_risk_pct is not None and policy.get("risk_model") != "fixed_volume":
            policy_risk_amount = equity * max(0.0, float(policy_risk_pct)) / 100
            checks.append(("Policy setup risk", proposed_setup_risk <= policy_risk_amount + 1e-6, f"proposed=${proposed_setup_risk:.2f}; policy=${policy_risk_amount:.2f}"))

        all_passed = all(passed for _, passed, _ in checks)
        failed = [name for name, passed, _ in checks if not passed]
        return RiskCheckResult(all_passed, f"Failed: {', '.join(failed)}" if failed else "Integrity and active policy checks passed", checks)


__all__ = [
    "BasketRiskSnapshot",
    "LayerSpec",
    "PositionSizingResult",
    "RiskCheckResult",
    "RiskManager",
]
