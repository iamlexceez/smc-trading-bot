"""
Risk management layer — hard gate checks before any trade execution.

Checks:
1. Auto-trade is enabled and not paused
2. Symbol is in the allowed symbols list
3. Symbol is not in cooldown
4. Daily loss limit not exceeded
5. Daily trade count not exceeded
6. Max open positions not exceeded
7. Score ≥ threshold
8. RR ≥ minimum
9. Spread within limit
10. Sufficient free margin
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from config import TradeSettings
from storage import db


@dataclass
class RiskCheckResult:
    passed: bool
    reason: str = ""
    checks: list[tuple[str, bool, str]] = None  # (name, passed, detail)


class RiskManager:
    """Evaluates all risk gates before trade execution."""

    def __init__(self, settings: TradeSettings):
        self.settings = settings

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
    ) -> RiskCheckResult:
        """Run all risk checks. Returns pass/fail with details."""
        checks = []

        # 1. Auto-trade enabled
        passed = self.settings.auto_trade and not self.settings.is_paused
        checks.append(("Auto-trade enabled", passed,
                       f"auto_trade={self.settings.auto_trade}, paused={self.settings.is_paused}"))

        # 2. Symbol allowed
        passed = symbol in self.settings.symbols
        checks.append(("Symbol allowed", passed,
                       f"{symbol} in {self.settings.symbols}"))

        # 3. Symbol cooldown
        in_cooldown = await db.is_symbol_in_cooldown(symbol, self.settings.symbol_cooldown_minutes)
        checks.append(("Symbol not in cooldown", not in_cooldown,
                       f"cooldown={self.settings.symbol_cooldown_minutes}min"))

        # 4. Daily loss limit (calculated against virtual balance if set, else real equity)
        base_equity = self.settings.virtual_balance if self.settings.virtual_balance else account_equity
        daily_loss_limit = base_equity * (self.settings.max_daily_loss_pct / 100)
        passed = today_pnl > -daily_loss_limit
        checks.append(("Daily loss limit", passed,
                       f"pnl={today_pnl:.2f}, limit=-{daily_loss_limit:.2f}"))

        # 5. Daily trade count
        passed = today_trade_count < self.settings.max_trades_per_day
        checks.append(("Daily trade count", passed,
                       f"{today_trade_count}/{self.settings.max_trades_per_day}"))

        # 6. Max open positions
        passed = open_position_count < self.settings.max_open_positions
        checks.append(("Max open positions", passed,
                       f"{open_position_count}/{self.settings.max_open_positions}"))

        # 7. Score threshold
        passed = score >= self.settings.score_threshold
        checks.append(("Score threshold", passed,
                       f"score={score:.1f}, threshold={self.settings.score_threshold:.1f}"))

        # 8. Minimum RR
        passed = rr_ratio >= self.settings.min_rr_ratio
        checks.append(("Min RR ratio", passed,
                       f"rr=1:{rr_ratio:.1f}, min=1:{self.settings.min_rr_ratio:.1f}"))

        # 9. Spread check
        passed = spread_pips <= self.settings.max_spread_pips
        checks.append(("Spread check", passed,
                       f"spread={spread_pips:.1f}pips, max={self.settings.max_spread_pips:.1f}pips"))

        # 10. Free margin
        passed = free_margin > required_margin * 2  # 2x safety buffer
        checks.append(("Free margin", passed,
                       f"free={free_margin:.2f}, required={required_margin:.2f}"))

        # Result
        all_passed = all(c[1] for c in checks)
        failed = [c[0] for c in checks if not c[1]]
        reason = f"Failed: {', '.join(failed)}" if failed else "All checks passed"

        return RiskCheckResult(passed=all_passed, reason=reason, checks=checks)

    def normalize_lot(self, lot: float, symbol_info: dict) -> float:
        """Normalize lot size to respect broker's min_lot, max_lot, and step_lot."""
        min_lot = symbol_info.get("min_lot", 0.01)
        max_lot = symbol_info.get("max_lot", 100.0)
        step_lot = symbol_info.get("step_lot", 0.01)
        
        # Adjust to step
        normalized = round(round(lot / step_lot) * step_lot, 2)
        
        # Clamp between min and max
        return max(min(normalized, max_lot), min_lot)

    def calculate_position_size(
        self,
        account_balance: float,
        entry_price: float,
        stop_loss: float,
        symbol_info: dict,
    ) -> float:
        """
        Calculate expert lot size based on risk percentage and symbol DNA.
        """
        base_balance = self.settings.virtual_balance if self.settings.virtual_balance else account_balance
        risk_amount = base_balance * (self.settings.risk_per_trade / 100)
        
        # Use tick_value and tick_size for precise calculation (Deriv style)
        tick_size = symbol_info.get("tick_size", 0.0001)
        tick_value = symbol_info.get("tick_value", 1.0)
        
        sl_distance_ticks = abs(entry_price - stop_loss) / tick_size
        if sl_distance_ticks <= 0:
            return 0.0

        # lot = risk / (ticks * tick_value)
        lot_size = risk_amount / (sl_distance_ticks * tick_value)
        
        return self.normalize_lot(lot_size, symbol_info)

    def get_layering_plan(self, total_lot: float, entry: float, sl: float, symbol_info: dict) -> list[dict]:
        """Split a trade into 3 expert layers."""
        if total_lot < symbol_info.get("min_lot", 0.01) * 3:
            # Too small to layer, return single entry
            return [{"price": entry, "lot": total_lot, "comment": "SMC Single"}]
            
        # Split: 40% at entry, 30% at 50% level, 30% at extreme
        # For simplicity in this version, we'll use price levels: entry, mid, extreme
        extreme = entry - (entry - sl) * 0.2 # 80% deep into the SL zone
        mid = (entry + extreme) / 2
        
        layers = [
            {"price": entry, "lot": self.normalize_lot(total_lot * 0.4, symbol_info), "comment": "Layer 1 (Agg)"},
            {"price": mid, "lot": self.normalize_lot(total_lot * 0.3, symbol_info), "comment": "Layer 2 (Bal)"},
            {"price": extreme, "lot": self.normalize_lot(total_lot * 0.3, symbol_info), "comment": "Layer 3 (Sniper)"},
        ]
        return layers
