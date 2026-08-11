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

        # 4. Daily loss limit (calculated against real account equity)
        daily_loss_limit = account_equity * (self.settings.max_daily_loss_pct / 100)
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

    def calculate_position_size(
        self,
        account_balance: float,
        entry_price: float,
        stop_loss: float,
        pip_size: float,
        contract_size: float = 100000,
    ) -> float:
        """
        Calculate lot size based on risk percentage.
        lot = (balance * risk%) / (SL_distance_in_pips * pip_value_per_lot)
        """
        risk_amount = account_balance * (self.settings.risk_per_trade / 100)
        sl_distance_pips = abs(entry_price - stop_loss) / pip_size
        if sl_distance_pips <= 0:
            return 0.0

        # Pip value per standard lot (approximate)
        pip_value_per_lot = contract_size * pip_size

        lot_size = risk_amount / (sl_distance_pips * pip_value_per_lot)

        # Round to nearest 0.01
        lot_size = round(lot_size, 2)

        # Safety: cap at 10% of balance worth
        max_lot = (account_balance * 0.1) / (contract_size * entry_price) if entry_price > 0 else 0
        lot_size = min(lot_size, max(max_lot, 0.01))

        return max(lot_size, 0.01)  # Minimum 0.01 lots
