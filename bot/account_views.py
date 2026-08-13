"""Read-only Telegram account dashboard views backed by a fresh MT5 snapshot."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from analysis.account_monitor import AccountReconciliationEngine, exposure_summary, summarize_history
from storage import db


class LiveAccountViews:
    """Format current broker data; never mutate orders, positions, or settings."""

    def __init__(self, executor: Any, account_mode: str) -> None:
        self.executor = executor
        self.account_mode = account_mode
        self.engine = AccountReconciliationEngine(executor, account_mode)

    @property
    def mode_label(self) -> str:
        return "DEMO" if self.account_mode == "demo" else "LIVE"

    @staticmethod
    def _money(value: Any, currency: str = "USD") -> str:
        try:
            return f"{currency} {float(value or 0.0):,.2f}"
        except (TypeError, ValueError):
            return f"{currency} 0.00"

    @staticmethod
    def _number(value: Any, decimals: int = 5) -> str:
        try:
            return f"{float(value):,.{decimals}f}"
        except (TypeError, ValueError):
            return "N/A"

    @staticmethod
    def _mask_login(value: Any) -> str:
        raw = str(value or "")
        return f"••••{raw[-4:]}" if len(raw) > 4 else raw or "N/A"

    async def _annotated_snapshot(self, days: int = 1) -> tuple[dict, dict]:
        snapshot = await self.engine.snapshot(history_days=days)
        if not snapshot.get("current"):
            return snapshot, {}
        bot_rows = {int(row["ticket"]): row for row in await db.get_open_trades(self.account_mode) if row.get("ticket") is not None}
        baskets = {ticket: await db.get_basket_for_ticket(ticket, self.account_mode) for ticket in bot_rows}
        snapshot["positions"] = self.engine.annotate_positions(snapshot, bot_rows, baskets)
        reconciliation = await self.engine.reconcile(snapshot)
        return snapshot, reconciliation

    async def account_overview(self) -> str:
        snapshot, reconciliation = await self._annotated_snapshot(days=1)
        if not snapshot.get("current"):
            return f"ACCOUNT — {self.mode_label}\n\nBROKER DATA UNAVAILABLE\n{snapshot.get('error', 'Unknown MT5 error')}"
        account = snapshot["account"]
        currency = str(account.get("currency") or "USD")
        balance = float(account.get("balance") or 0.0)
        equity = float(account.get("equity") or 0.0)
        floating = float(account.get("profit") or equity - balance)
        history = summarize_history(snapshot.get("history", []))
        drawdown = max(0.0, balance - equity) / balance * 100 if balance else 0.0
        exposure = exposure_summary(snapshot)
        capital = await db.get_account_state("demo") if self.account_mode == "demo" else None
        capital_status = str((capital or {}).get("state") or "NOT YET VERIFIED")
        minimum_operating = self._money((capital or {}).get("minimum_operating_capital"), currency)
        capital_reason = (capital or {}).get("exhaustion_reason") or "Broker state currently supports normal monitoring."
        return "\n".join([
            f"ACCOUNT — {self.mode_label} — LIVE MT5 DATA",
            f"Updated: {snapshot.get('retrieved_at')}",
            f"Account: {self._mask_login(account.get('login'))} | Server: {account.get('server', 'N/A')}",
            f"Currency: {currency}",
            "",
            f"Balance: {self._money(balance, currency)}",
            f"Equity: {self._money(equity, currency)}",
            f"Floating P/L: {self._money(floating, currency)}",
            f"Today realized P/L: {self._money(history['net_profit'], currency)}",
            f"Free margin: {self._money(account.get('free_margin'), currency)}",
            f"Used margin: {self._money(account.get('margin'), currency)}",
            f"Margin level: {float(account.get('margin_level') or 0.0):.1f}%",
            "",
            f"Open positions: {len(snapshot.get('positions', []))} | Pending orders: {len(snapshot.get('pending_orders', []))}",
            f"Margin exposure: {exposure['margin_exposure_pct']:.2f}% | Current drawdown vs balance: {drawdown:.2f}%",
            f"Capital state: {capital_status} | Minimum operating capital: {minimum_operating}",
            f"Capital action: {capital_reason}",
            f"Account sync: {'SYNCHRONIZED' if not reconciliation.get('discrepancies') else f'{len(reconciliation.get("discrepancies", []))} discrepancy(s) — use /health'}",
            "Source: fresh MT5 account snapshot; local database is used only for bot-trade annotation.",
        ])

    async def positions(self) -> list[str]:
        snapshot, _ = await self._annotated_snapshot(days=1)
        if not snapshot.get("current"):
            return [f"POSITIONS — {self.mode_label}\n\nBROKER DATA UNAVAILABLE\n{snapshot.get('error', 'Unknown MT5 error')}"]
        currency = str(snapshot["account"].get("currency") or "USD")
        positions = snapshot.get("positions", [])
        if not positions:
            return [f"OPEN POSITIONS — {self.mode_label}\n\nMT5 reports no open positions. Updated: {snapshot.get('retrieved_at')}"]
        views = [f"OPEN POSITIONS — {self.mode_label} — LIVE MT5 DATA\nUpdated: {snapshot.get('retrieved_at')}"]
        for position in positions:
            owner = "BOT" if position.get("ownership") == "bot" else "MANUAL / UNTRACKED"
            r_value = position.get("r_multiple")
            r_text = f" | R: {float(r_value):+.2f}R" if r_value is not None else ""
            provenance = ""
            if owner == "BOT":
                provenance = f"\nPolicy: {position.get('policy_version') or 'N/A'} | Experiment: {position.get('experiment_id') or 'N/A'} | Basket: {position.get('basket_id') or 'N/A'} | Layer: {position.get('layer_number') or 'N/A'}"
            views.append("\n".join([
                f"#{position['ticket']} — {position['symbol']} {position['direction']} — {owner}",
                f"Volume: {position['volume']} | Entry: {self._number(position.get('entry_price'))} | Current: {self._number(position.get('current_price'))}",
                f"SL: {self._number(position.get('sl'))} | TP: {self._number(position.get('tp'))}",
                f"P/L: {self._money(position.get('profit'), currency)}{r_text}",
                f"Distance → SL: {self._number(position.get('distance_to_sl'))} | TP: {self._number(position.get('distance_to_tp'))}",
                f"Opened: {position.get('open_time') or 'N/A'}{provenance}",
            ]))
        return views

    async def position_detail(self, ticket: int) -> str:
        snapshot, _ = await self._annotated_snapshot(days=1)
        if not snapshot.get("current"):
            return f"POSITION #{ticket} — {self.mode_label}\n\nBROKER DATA UNAVAILABLE\n{snapshot.get('error', 'Unknown MT5 error')}"
        position = next((row for row in snapshot.get("positions", []) if int(row.get("ticket", 0)) == ticket), None)
        if not position:
            return f"POSITION #{ticket} — {self.mode_label}\n\nNot present in the current MT5 position list."
        currency = str(snapshot["account"].get("currency") or "USD")
        owner = "BOT" if position.get("ownership") == "bot" else "MANUAL / UNTRACKED"
        management = "Not bot-managed" if owner != "BOT" else "Policy-managed; see bot action log via /positions"
        return "\n".join([
            f"POSITION #{ticket} — {self.mode_label} — LIVE MT5 DATA",
            f"Symbol: {position['symbol']} | Direction: {position['direction']} | Ownership: {owner}",
            f"Volume: {position['volume']} | Opened: {position.get('open_time') or 'N/A'}",
            "",
            f"Entry: {self._number(position.get('entry_price'))} | Current: {self._number(position.get('current_price'))}",
            f"SL: {self._number(position.get('sl'))} | TP: {self._number(position.get('tp'))}",
            f"Unrealized P/L: {self._money(position.get('profit'), currency)}",
            f"Current R: {f'{float(position.get("r_multiple")):+.2f}R' if position.get('r_multiple') is not None else 'N/A (manual/untracked or initial risk unavailable)'}",
            f"Distance → SL: {self._number(position.get('distance_to_sl'))} | TP: {self._number(position.get('distance_to_tp'))}",
            f"Potential SL P/L: {self._money(position.get('potential_sl'), currency)} | Potential TP P/L: {self._money(position.get('potential_tp'), currency)}",
            "",
            f"Management status: {management}",
            f"Policy: {position.get('policy_version') or 'N/A'} | Experiment: {position.get('experiment_id') or 'N/A'} | Setup: {position.get('setup_id') or 'N/A'} | Basket: {position.get('basket_id') or 'N/A'} | Layer: {position.get('layer_number') or 'N/A'}",
        ])

    async def orders(self) -> str:
        snapshot, _ = await self._annotated_snapshot(days=1)
        if not snapshot.get("current"):
            return f"PENDING ORDERS — {self.mode_label}\n\nBROKER DATA UNAVAILABLE\n{snapshot.get('error', 'Unknown MT5 error')}"
        orders = snapshot.get("pending_orders", [])
        if not orders:
            return f"PENDING ORDERS — {self.mode_label}\n\nMT5 reports no pending orders. Updated: {snapshot.get('retrieved_at')}"
        lines = [f"PENDING ORDERS — {self.mode_label} — LIVE MT5 DATA", f"Updated: {snapshot.get('retrieved_at')}", ""]
        for order in orders[:25]:
            lines.extend([
                f"#{order['ticket']} — {order['symbol']} {order['type']}",
                f"Volume: {order['volume']} | Entry: {self._number(order.get('entry_price'))} | Current: {self._number(order.get('current_price'))}",
                f"Distance to entry: {self._number(order.get('distance_to_entry'))} | SL: {self._number(order.get('sl'))} | TP: {self._number(order.get('tp'))}",
                f"Created: {order.get('created_at') or 'N/A'} | Expires: {order.get('expiration') or 'N/A'}",
                "",
            ])
        if len(orders) > 25:
            lines.append(f"… {len(orders) - 25} additional MT5 pending orders omitted from Telegram preview.")
        return "\n".join(lines)

    async def history(self, days: int) -> str:
        snapshot, _ = await self._annotated_snapshot(days=days)
        if not snapshot.get("current"):
            return f"TRADE HISTORY — {self.mode_label}\n\nBROKER DATA UNAVAILABLE\n{snapshot.get('error', 'Unknown MT5 error')}"
        currency = str(snapshot["account"].get("currency") or "USD")
        deals = snapshot.get("history", [])
        stats = summarize_history(deals)
        factor = "∞" if stats["profit_factor"] == float("inf") else f"{stats['profit_factor']:.2f}"
        lines = [
            f"TRADE HISTORY — {self.mode_label} — MT5 CLOSED DEALS ({days} day(s))",
            f"Trades: {stats['trades']} | Wins: {stats['wins']} | Losses: {stats['losses']} | Win rate: {stats['win_rate']:.1f}%",
            f"Gross profit: {self._money(stats['gross_profit'], currency)} | Gross loss: {self._money(stats['gross_loss'], currency)}",
            f"Net: {self._money(stats['net_profit'], currency)} | PF: {factor} | Expectancy: {self._money(stats['expectancy'], currency)}",
            "",
        ]
        for deal in deals[-15:]:
            lines.append(f"#{deal['ticket']} {deal['symbol']} vol {deal['volume']} | {self._money(deal['net_profit'], currency)} | {deal['time']}")
        return "\n".join(lines)

    async def daily_performance(self) -> str:
        """Show current-day broker deals plus current account floating P/L."""
        snapshot, _ = await self._annotated_snapshot(days=1)
        if not snapshot.get("current"):
            return f"DAILY PERFORMANCE — {self.mode_label}\n\nBROKER DATA UNAVAILABLE\n{snapshot.get('error', 'Unknown MT5 error')}"
        account = snapshot["account"]
        currency = str(account.get("currency") or "USD")
        balance = float(account.get("balance") or 0.0)
        equity = float(account.get("equity") or 0.0)
        floating = float(account.get("profit") or equity - balance)
        stats = summarize_history(snapshot.get("history", []))
        net = stats["net_profit"] + floating
        reference = balance - stats["net_profit"]
        return_pct = net / reference * 100 if reference else 0.0
        factor = "∞" if stats["profit_factor"] == float("inf") else f"{stats['profit_factor']:.2f}"
        current_drawdown = max(0.0, balance - equity) / balance * 100 if balance else 0.0
        return "\n".join([
            f"DAILY PERFORMANCE — {self.mode_label} — LIVE MT5 DATA",
            f"Updated: {snapshot.get('retrieved_at')}",
            f"Current balance: {self._money(balance, currency)} | Equity: {self._money(equity, currency)}",
            f"Realized P/L today: {self._money(stats['net_profit'], currency)} | Floating P/L: {self._money(floating, currency)} | Net: {self._money(net, currency)}",
            f"Today return: {return_pct:.2f}% (calculated from current balance and MT5 closed deals)",
            f"Closed deals: {stats['trades']} | Win rate: {stats['win_rate']:.1f}% | PF: {factor} | Expectancy: {self._money(stats['expectancy'], currency)}",
            f"Largest win: {self._money(stats['largest_win'], currency)} | Largest loss: {self._money(stats['largest_loss'], currency)}",
            f"Current drawdown vs balance: {current_drawdown:.2f}%",
            "Source: fresh MT5 account and MT5 deal history. A historical day-start balance is not guessed when MT5 does not expose it directly.",
        ])

    async def exposure(self) -> str:
        snapshot, _ = await self._annotated_snapshot(days=1)
        if not snapshot.get("current"):
            return f"EXPOSURE — {self.mode_label}\n\nBROKER DATA UNAVAILABLE\n{snapshot.get('error', 'Unknown MT5 error')}"
        currency = str(snapshot["account"].get("currency") or "USD")
        summary = exposure_summary(snapshot)
        lines = [
            f"EXPOSURE — {self.mode_label} — LIVE MT5 DATA",
            f"Open positions: {summary['open_positions']} | Margin exposure: {summary['margin_exposure_pct']:.2f}%",
            f"Potential SL P/L: {self._money(summary['potential_sl'], currency)} | Potential TP P/L: {self._money(summary['potential_tp'], currency)}",
            f"Used margin: {self._money(summary['used_margin'], currency)} | Free margin: {self._money(summary['free_margin'], currency)} | Margin level: {summary['margin_level']:.1f}%",
            "",
            "By symbol (open volume):",
            *( [f"- {symbol}: {volume}" for symbol, volume in summary['by_symbol'].items()] or ["- None"] ),
            "By direction (open volume):",
            *( [f"- {side}: {volume}" for side, volume in summary['by_direction'].items()] or ["- None"] ),
        ]
        return "\n".join(lines)

    async def health(self) -> str:
        snapshot, reconciliation = await self._annotated_snapshot(days=1)
        if not snapshot.get("current"):
            return f"ACCOUNT HEALTH — {self.mode_label}\n\nCONNECTION LOST / DATA UNAVAILABLE\n{snapshot.get('error', 'Unknown MT5 error')}"
        account = snapshot["account"]
        discrepancies = reconciliation.get("discrepancies", [])
        capital = await db.get_account_state("demo") if self.account_mode == "demo" else None
        return "\n".join([
            f"ACCOUNT HEALTH — {self.mode_label}",
            "Connection: CONNECTED | Data: CURRENT (fresh MT5 query)",
            f"Open positions: {len(snapshot.get('positions', []))} | Pending orders: {len(snapshot.get('pending_orders', []))}",
            f"Free margin: {self._money(account.get('free_margin'), str(account.get('currency') or 'USD'))} | Margin level: {float(account.get('margin_level') or 0.0):.1f}%",
            f"Capital state: {(capital or {}).get('state') or 'NOT YET VERIFIED'} | Minimum operating capital: {self._money((capital or {}).get('minimum_operating_capital'), str(account.get('currency') or 'USD'))}",
            f"Capital reason: {(capital or {}).get('exhaustion_reason') or 'No blocking capital condition recorded.'}",
            f"Account synchronization: {'SYNCHRONIZED' if not discrepancies else f'{len(discrepancies)} discrepancy(s) detected'}",
            *( [f"- #{item.get('ticket')} {item.get('symbol')}: {item.get('detail')}" for item in discrepancies[:10]] or ["- No broker/local mismatch detected."] ),
            f"Last synchronization: {snapshot.get('retrieved_at')}",
            "Monitoring is read-only; discrepancies are reported and never auto-modified.",
        ])
