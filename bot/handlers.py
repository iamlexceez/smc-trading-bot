"""
Telegram bot command handlers.

Commands:
/start          — Show main menu
/scan           — Scan all symbols for signals
/analyze [sym]  — Deep analysis of a specific symbol
/positions      — Show open positions
/close_all      — Close all positions (with confirmation)
/settings       — Adjust all settings via inline keyboards
/account        — Show account info
/history        — Show recent trade history
/pause          — Pause auto-trading
/resume         — Resume auto-trading
/mode [paper|live] — Switch execution mode
/risk [pct]     — Set risk per trade
/rr [ratio]     — Set minimum RR ratio
/score [val]    — Set score threshold
/help           — Show all commands
"""

from __future__ import annotations

import logging
import json
from typing import Optional

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    CommandHandler, CallbackQueryHandler, MessageHandler,
    ContextTypes, filters, Application,
)

from config import TradeSettings, get_admin_ids
from bot import keyboards
from storage import db
from analysis.scoring import format_signal_report, TradeSignal
from risk.manager import RiskManager

logger = logging.getLogger(__name__)


def is_admin(user_id: int) -> bool:
    """Check if user is in the admin allowlist."""
    return user_id in get_admin_ids()


def admin_only(func):
    """Decorator to restrict commands to admin users. Works with class methods."""
    async def wrapper(self, update: Update, context: ContextTypes.DEFAULT_TYPE, *args, **kwargs):
        if not update.effective_user:
            return
        if not is_admin(update.effective_user.id):
            if update.message:
                await update.message.reply_text(
                    "⛔ You are not authorized to use this bot.\n"
                    "Add your Telegram ID to TELEGRAM_ADMIN_IDS in .env"
                )
            return
        return await func(self, update, context, *args, **kwargs)
    return wrapper


class BotHandlers:
    """Holds shared state for bot handlers."""

    def __init__(self, settings: TradeSettings, executor, risk_manager: RiskManager, scheduler=None):
        self.settings = settings
        self.executor = executor
        self.risk_manager = risk_manager
        self.scheduler = scheduler
        self.app: Optional[Application] = None

    async def reload_settings(self):
        """Reload settings from DB."""
        self.settings = await db.load_settings()
        self.risk_manager.settings = self.settings

    # ─── Commands ──────────────────────────────────────────

    @admin_only
    async def cmd_start(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Show the main menu."""
        await self.reload_settings()
        text = (
            f"🤖 **SMC Trading Bot**\n\n"
            f"Mode: `{self.settings.trading_mode}`\n"
            f"Auto-Trade: {'✅ ON' if self.settings.auto_trade else '❌ OFF'}\n"
            f"Paused: {'⏸ YES' if self.settings.is_paused else '▶️ NO'}\n\n"
            f"Risk/trade: {self.settings.risk_per_trade}%\n"
            f"Min RR: 1:{self.settings.min_rr_ratio}\n"
            f"Score threshold: {self.settings.score_threshold}%\n\n"
            f"Symbols: {', '.join(self.settings.symbols[:5])}{'...' if len(self.settings.symbols) > 5 else ''}\n"
            f"Timeframes: {', '.join(self.settings.timeframes)}\n\n"
            f"Choose an option below 👇"
        )
        await update.message.reply_text(text, reply_markup=keyboards.main_menu())

    @admin_only
    async def cmd_scan(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Trigger a market scan."""
        if self.scheduler:
            await update.message.reply_text("🔍 Scanning markets... This may take a moment.")
            results = await self.scheduler.scan_markets()
            if not results:
                await update.message.reply_text("No signals found in this scan.")
                return
            for signal in results:
                await update.message.reply_text(format_signal_report(signal))
        else:
            await update.message.reply_text("Scheduler not initialized.")

    @admin_only
    async def cmd_analyze(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Show analysis menu or analyze a specific symbol."""
        if context.args:
            symbol = " ".join(context.args)
            await self._do_analysis(update, symbol)
        else:
            await update.message.reply_text(
                "Select a symbol to analyze:",
                reply_markup=keyboards.analysis_menu(self.settings.symbols)
            )

    async def _do_analysis(self, update: Update, symbol: str):
        """Perform deep analysis on a symbol."""
        if self.scheduler:
            await update.message.reply_text(f"📊 Analyzing {symbol}...")
            signal = await self.scheduler.analyze_symbol(symbol)
            if signal:
                await update.message.reply_text(format_signal_report(signal))
            else:
                await update.message.reply_text(f"No tradeable signal for {symbol} at this time.")
        else:
            await update.message.reply_text("Scheduler not initialized.")

    @admin_only
    async def cmd_positions(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Show open positions."""
        positions = await self.executor.get_open_positions()
        if not positions:
            await update.message.reply_text("No open positions.")
            return

        lines = ["📈 **Open Positions**\n"]
        total_profit = 0
        for p in positions:
            emoji = "🟢" if p.profit >= 0 else "🔴"
            lines.append(
                f"{emoji} #{p.ticket} | {p.direction} {p.volume} {p.symbol}\n"
                f"   Entry: {p.entry_price:.5f} | SL: {p.sl:.5f} | TP: {p.tp:.5f}\n"
                f"   PnL: ${p.profit:.2f}"
            )
            total_profit += p.profit
        lines.append(f"\n**Total PnL: ${total_profit:.2f}**")
        await update.message.reply_text("\n".join(lines))

    @admin_only
    async def cmd_close_all(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Close all positions with confirmation."""
        positions = await self.executor.get_open_positions()
        if not positions:
            await update.message.reply_text("No open positions to close.")
            return
        await update.message.reply_text(
            f"⚠️ Close all {len(positions)} open positions?\nThis action cannot be undone.",
            reply_markup=keyboards.confirm_keyboard("close_all")
        )

    @admin_only
    async def cmd_settings(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Show settings menu."""
        await self.reload_settings()
        await update.message.reply_text(
            self._format_settings(),
            reply_markup=keyboards.settings_menu()
        )

    def _format_settings(self) -> str:
        return (
            "⚙️ **Current Settings**\n\n"
            f"Risk/trade: {self.settings.risk_per_trade}%\n"
            f"Max daily loss: {self.settings.max_daily_loss_pct}%\n"
            f"Max trades/day: {self.settings.max_trades_per_day}\n"
            f"Max positions: {self.settings.max_open_positions}\n"
            f"Min RR: 1:{self.settings.min_rr_ratio}\n"
            f"Score threshold: {self.settings.score_threshold}%\n"
            f"Max spread: {self.settings.max_spread_pips} pips\n"
            f"Cooldown: {self.settings.symbol_cooldown_minutes} min\n"
            f"Auto-trade: {'ON' if self.settings.auto_trade else 'OFF'}\n"
            f"Mode: {self.settings.trading_mode}\n"
            f"Symbols: {', '.join(self.settings.symbols)}\n"
            f"Timeframes: {', '.join(self.settings.timeframes)}"
        )

    @admin_only
    async def cmd_account(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Show account info."""
        info = await self.executor.get_account_info()
        if not info:
            await update.message.reply_text("Could not retrieve account info.")
            return
        await update.message.reply_text(
            f"💰 **Account Info**\n\n"
            f"Balance: ${info.get('balance', 0):.2f}\n"
            f"Equity: ${info.get('equity', 0):.2f}\n"
            f"Free Margin: ${info.get('free_margin', 0):.2f}\n"
            f"Used Margin: ${info.get('margin', 0):.2f}\n"
            f"Leverage: 1:{info.get('leverage', 0)}\n"
            f"Currency: {info.get('currency', 'USD')}\n"
            f"Server: {info.get('server', 'N/A')}"
        )

    @admin_only
    async def cmd_history(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Show recent trade history."""
        trades = await db.get_trade_history(limit=15)
        if not trades:
            await update.message.reply_text("No trade history yet.")
            return

        lines = ["📋 **Recent Trades**\n"]
        for t in trades:
            emoji = "✅" if t["pnl"] >= 0 else "❌"
            status_emoji = "🟢" if t["status"] == "open" else "⚫"
            lines.append(
                f"{emoji} {status_emoji} {t['direction']} {t['symbol']}\n"
                f"   Score: {t['score']:.1f} | RR: 1:{t['rr_ratio']:.1f} | "
                f"PnL: ${t['pnl']:.2f} | {t['executor']}"
            )
        await update.message.reply_text("\n".join(lines))

    @admin_only
    async def cmd_pause(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Pause auto-trading."""
        self.settings.is_paused = True
        await db.save_settings(self.settings)
        await update.message.reply_text("⏸ Auto-trading paused. Use /resume to continue.")

    @admin_only
    async def cmd_resume(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Resume auto-trading."""
        self.settings.is_paused = False
        await db.save_settings(self.settings)
        await update.message.reply_text("▶️ Auto-trading resumed.")

    @admin_only
    async def cmd_mode(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Switch execution mode."""
        if context.args and context.args[0].lower() in ("paper", "live"):
            mode = context.args[0].lower()
            if mode == "live":
                await update.message.reply_text(
                    "⚠️ Switching to LIVE mode will execute real trades.\n"
                    "Are you sure?",
                    reply_markup=keyboards.confirm_keyboard("mode_live")
                )
            else:
                self.settings.trading_mode = "paper"
                await db.save_settings(self.settings)
                await update.message.reply_text("✅ Switched to PAPER mode.")
        else:
            await update.message.reply_text(
                f"Current mode: {self.settings.trading_mode}\nUsage: /mode paper or /mode live"
            )

    @admin_only
    async def cmd_risk(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Set risk per trade."""
        if context.args:
            try:
                val = float(context.args[0])
                self.settings.risk_per_trade = max(0.1, min(val, 10.0))
                await db.save_settings(self.settings)
                await update.message.reply_text(f"✅ Risk per trade set to {self.settings.risk_per_trade}%")
            except ValueError:
                await update.message.reply_text("Usage: /risk 1.0")
        else:
            await update.message.reply_text(f"Current risk: {self.settings.risk_per_trade}%\nUsage: /risk 1.5")

    @admin_only
    async def cmd_rr(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Set minimum RR ratio."""
        if context.args:
            try:
                val = float(context.args[0])
                self.settings.min_rr_ratio = max(1.0, min(val, 20.0))
                await db.save_settings(self.settings)
                await update.message.reply_text(f"✅ Min RR set to 1:{self.settings.min_rr_ratio}")
            except ValueError:
                await update.message.reply_text("Usage: /rr 3.0")
        else:
            await update.message.reply_text(f"Current min RR: 1:{self.settings.min_rr_ratio}\nUsage: /rr 3.0")

    @admin_only
    async def cmd_score(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Set score threshold."""
        if context.args:
            try:
                val = float(context.args[0])
                self.settings.score_threshold = max(1.0, min(val, 100.0))
                await db.save_settings(self.settings)
                await update.message.reply_text(f"✅ Score threshold set to {self.settings.score_threshold}%")
            except ValueError:
                await update.message.reply_text("Usage: /score 40")
        else:
            await update.message.reply_text(f"Current threshold: {self.settings.score_threshold}%\nUsage: /score 40")

    @admin_only
    async def cmd_help(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Show help."""
        await update.message.reply_text(
            "🤖 **SMC Trading Bot — Commands**\n\n"
            "/start — Main menu\n"
            "/scan — Scan all symbols for signals\n"
            "/analyze [symbol] — Deep analysis of a symbol\n"
            "/positions — Show open positions\n"
            "/close_all — Close all positions\n"
            "/settings — Adjust all settings\n"
            "/account — Account info\n"
            "/history — Recent trades\n"
            "/pause — Pause auto-trading\n"
            "/resume — Resume auto-trading\n"
            "/mode [paper|live] — Switch execution mode\n"
            "/risk [pct] — Set risk per trade\n"
            "/rr [ratio] — Set min RR ratio\n"
            "/score [val] — Set score threshold\n"
            "/help — This message"
        )

    # ─── Callback handlers ─────────────────────────────────

    async def handle_callback(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle inline keyboard callbacks."""
        if not is_admin(update.effective_user.id):
            return

        query = update.callback_query
        await query.answer()
        data = query.data

        await self.reload_settings()

        if data == "main":
            await query.edit_message_text(
                "🤖 SMC Trading Bot — Main Menu",
                reply_markup=keyboards.main_menu()
            )
        elif data == "settings":
            await query.edit_message_text(
                self._format_settings(),
                reply_markup=keyboards.settings_menu()
            )
        elif data == "scan":
            await query.edit_message_text("🔍 Scanning markets...")
            if self.scheduler:
                results = await self.scheduler.scan_markets()
                if results:
                    for signal in results:
                        await context.bot.send_message(
                            query.message.chat_id,
                            format_signal_report(signal)
                        )
                else:
                    await context.bot.send_message(query.message.chat_id, "No signals found.")
            await context.bot.send_message(
                query.message.chat_id, "Back to menu:",
                reply_markup=keyboards.main_menu()
            )
        elif data == "positions":
            positions = await self.executor.get_open_positions()
            if not positions:
                text = "No open positions."
            else:
                lines = ["📈 **Open Positions**\n"]
                total = 0
                for p in positions:
                    emoji = "🟢" if p.profit >= 0 else "🔴"
                    lines.append(f"{emoji} #{p.ticket} | {p.direction} {p.volume} {p.symbol} | PnL: ${p.profit:.2f}")
                    total += p.profit
                lines.append(f"\n**Total: ${total:.2f}**")
                text = "\n".join(lines)
            await query.edit_message_text(text, reply_markup=keyboards.main_menu())
        elif data == "history":
            trades = await db.get_trade_history(limit=10)
            if not trades:
                text = "No trade history."
            else:
                lines = ["📋 **Recent Trades**\n"]
                for t in trades:
                    emoji = "✅" if t["pnl"] >= 0 else "❌"
                    lines.append(f"{emoji} {t['direction']} {t['symbol']} | Score: {t['score']:.0f} | PnL: ${t['pnl']:.2f}")
                text = "\n".join(lines)
            await query.edit_message_text(text, reply_markup=keyboards.main_menu())
        elif data == "account":
            info = await self.executor.get_account_info()
            text = (
                f"💰 **Account**\n\n"
                f"Balance: ${info.get('balance', 0):.2f}\n"
                f"Equity: ${info.get('equity', 0):.2f}\n"
                f"Free Margin: ${info.get('free_margin', 0):.2f}"
            )
            await query.edit_message_text(text, reply_markup=keyboards.main_menu())
        elif data == "pause":
            self.settings.is_paused = True
            await db.save_settings(self.settings)
            await query.edit_message_text("⏸ Auto-trading paused.", reply_markup=keyboards.main_menu())
        elif data == "resume":
            self.settings.is_paused = False
            await db.save_settings(self.settings)
            await query.edit_message_text("▶️ Auto-trading resumed.", reply_markup=keyboards.main_menu())
        elif data == "close_all":
            positions = await self.executor.get_open_positions()
            if not positions:
                await query.edit_message_text("No open positions to close.", reply_markup=keyboards.main_menu())
            else:
                await query.edit_message_text(
                    f"⚠️ Close all {len(positions)} open positions?\nThis cannot be undone.",
                    reply_markup=keyboards.confirm_keyboard("close_all")
                )
        elif data == "confirm_close_all":
            count = await self.executor.close_all_positions()
            await query.edit_message_text(f"✅ Closed {count} positions.", reply_markup=keyboards.main_menu())
        elif data == "cancel":
            await query.edit_message_text("Action cancelled.", reply_markup=keyboards.main_menu())
        elif data == "set_autotrade":
            await query.edit_message_text(
                f"Auto-Trade is currently {'ON ✅' if self.settings.auto_trade else 'OFF ❌'}",
                reply_markup=keyboards.autotrade_menu(self.settings.auto_trade)
            )
        elif data == "toggle_autotrade":
            if not self.settings.auto_trade:
                # Turning ON — require confirmation
                await query.edit_message_text(
                    "⚠️ Enable auto-trade? The bot will automatically execute trades that pass all risk gates.",
                    reply_markup=keyboards.confirm_keyboard("autotrade_on")
                )
            else:
                # Turning OFF — no confirmation needed
                self.settings.auto_trade = False
                await db.save_settings(self.settings)
                await query.edit_message_text(
                    "Auto-Trade is now OFF ❌",
                    reply_markup=keyboards.autotrade_menu(self.settings.auto_trade)
                )
        elif data == "confirm_autotrade_on":
            self.settings.auto_trade = True
            await db.save_settings(self.settings)
            await query.edit_message_text(
                "Auto-Trade is now ON ✅",
                reply_markup=keyboards.main_menu()
            )
        elif data == "set_mode":
            await query.edit_message_text(
                f"Current mode: {self.settings.trading_mode}",
                reply_markup=keyboards.mode_menu(self.settings.trading_mode)
            )
        elif data == "mode_paper":
            self.settings.trading_mode = "paper"
            await db.save_settings(self.settings)
            await query.edit_message_text("✅ Switched to PAPER mode.", reply_markup=keyboards.settings_menu())
        elif data == "mode_live":
            await query.edit_message_text(
                "⚠️ Switch to LIVE mode? Real trades will execute with real money.",
                reply_markup=keyboards.confirm_keyboard("mode_live")
            )
        elif data == "confirm_mode_live":
            self.settings.trading_mode = "live"
            await db.save_settings(self.settings)
            await query.edit_message_text("⚠️ LIVE mode enabled. Real trades will execute.", reply_markup=keyboards.settings_menu())
        elif data == "set_risk":
            await query.edit_message_text(
                f"Current risk per trade: {self.settings.risk_per_trade}%\nUse command: /risk 1.5\n(Range: 0.1% - 10%)",
                reply_markup=keyboards.settings_menu()
            )
        elif data == "set_rr":
            await query.edit_message_text(
                f"Current min RR: 1:{self.settings.min_rr_ratio}\nUse command: /rr 3.0\n(Range: 1.0 - 20.0)",
                reply_markup=keyboards.settings_menu()
            )
        elif data == "set_score":
            await query.edit_message_text(
                f"Current score threshold: {self.settings.score_threshold}%\nUse command: /score 40\n(Range: 1 - 100)",
                reply_markup=keyboards.settings_menu()
            )
        elif data == "set_spread":
            old = self.settings.max_spread_pips
            self.settings.max_spread_pips = 10.0 if old < 10.0 else 3.0
            await db.save_settings(self.settings)
            await query.edit_message_text(
                f"Max spread set to {self.settings.max_spread_pips} pips.",
                reply_markup=keyboards.settings_menu()
            )
        elif data == "set_daily_loss":
            old = self.settings.max_daily_loss_pct
            self.settings.max_daily_loss_pct = 10.0 if old < 10.0 else 3.0
            await db.save_settings(self.settings)
            await query.edit_message_text(
                f"Max daily loss set to {self.settings.max_daily_loss_pct}%.",
                reply_markup=keyboards.settings_menu()
            )
        elif data == "set_max_trades":
            old = self.settings.max_trades_per_day
            self.settings.max_trades_per_day = 20 if old < 20 else 5
            await db.save_settings(self.settings)
            await query.edit_message_text(
                f"Max trades/day set to {self.settings.max_trades_per_day}.",
                reply_markup=keyboards.settings_menu()
            )
        elif data == "set_max_pos":
            old = self.settings.max_open_positions
            self.settings.max_open_positions = 10 if old < 10 else 3
            await db.save_settings(self.settings)
            await query.edit_message_text(
                f"Max open positions set to {self.settings.max_open_positions}.",
                reply_markup=keyboards.settings_menu()
            )
        elif data == "set_cooldown":
            old = self.settings.symbol_cooldown_minutes
            self.settings.symbol_cooldown_minutes = 60 if old < 60 else 15
            await db.save_settings(self.settings)
            await query.edit_message_text(
                f"Symbol cooldown set to {self.settings.symbol_cooldown_minutes} min.",
                reply_markup=keyboards.settings_menu()
            )
        elif data == "set_symbols":
            all_symbols = [
                "EURUSD", "GBPUSD", "USDJPY", "AUDUSD", "USDCAD", "XAUUSD",
                "Volatility 75 Index", "Volatility 100 Index",
                "Boom 500 Index", "Boom 1000 Index",
                "Crash 500 Index", "Crash 1000 Index",
            ]
            await query.edit_message_text(
                "Toggle symbols (✅ = active):",
                reply_markup=keyboards.symbol_select_keyboard(all_symbols, self.settings.symbols)
            )
        elif data.startswith("sym_"):
            sym = data.replace("sym_", "")
            if sym in self.settings.symbols:
                self.settings.symbols.remove(sym)
            else:
                self.settings.symbols.append(sym)
            await db.save_settings(self.settings)
            all_symbols = [
                "EURUSD", "GBPUSD", "USDJPY", "AUDUSD", "USDCAD", "XAUUSD",
                "Volatility 75 Index", "Volatility 100 Index",
                "Boom 500 Index", "Boom 1000 Index",
                "Crash 500 Index", "Crash 1000 Index",
            ]
            await query.edit_message_text(
                "Toggle symbols (✅ = active):",
                reply_markup=keyboards.symbol_select_keyboard(all_symbols, self.settings.symbols)
            )
        elif data == "set_timeframes":
            all_tfs = ["M5", "M15", "M30", "H1", "H4", "D1"]
            await query.edit_message_text(
                "Toggle timeframes (✅ = active):",
                reply_markup=keyboards.timeframe_select_keyboard(all_tfs, self.settings.timeframes)
            )
        elif data.startswith("tf_"):
            tf = data.replace("tf_", "")
            if tf in self.settings.timeframes:
                if len(self.settings.timeframes) > 1:
                    self.settings.timeframes.remove(tf)
            else:
                self.settings.timeframes.append(tf)
            await db.save_settings(self.settings)
            all_tfs = ["M5", "M15", "M30", "H1", "H4", "D1"]
            await query.edit_message_text(
                "Toggle timeframes (✅ = active):",
                reply_markup=keyboards.timeframe_select_keyboard(all_tfs, self.settings.timeframes)
            )
        elif data.startswith("analyze_"):
            symbol = data.replace("analyze_", "")
            await query.edit_message_text(f"📊 Analyzing {symbol}...")
            if self.scheduler:
                signal = await self.scheduler.analyze_symbol(symbol)
                if signal:
                    await context.bot.send_message(query.message.chat_id, format_signal_report(signal))
                else:
                    await context.bot.send_message(query.message.chat_id, f"No signal for {symbol}.")
            await context.bot.send_message(query.message.chat_id, "Menu:", reply_markup=keyboards.main_menu())
        else:
            await query.edit_message_text("Unknown action.", reply_markup=keyboards.main_menu())

    # ─── Setup ─────────────────────────────────────────────

    def setup(self, app: Application) -> None:
        """Register all handlers with the application."""
        app.add_handler(CommandHandler("start", self.cmd_start))
        app.add_handler(CommandHandler("help", self.cmd_help))
        app.add_handler(CommandHandler("scan", self.cmd_scan))
        app.add_handler(CommandHandler("analyze", self.cmd_analyze))
        app.add_handler(CommandHandler("positions", self.cmd_positions))
        app.add_handler(CommandHandler("close_all", self.cmd_close_all))
        app.add_handler(CommandHandler("settings", self.cmd_settings))
        app.add_handler(CommandHandler("account", self.cmd_account))
        app.add_handler(CommandHandler("history", self.cmd_history))
        app.add_handler(CommandHandler("pause", self.cmd_pause))
        app.add_handler(CommandHandler("resume", self.cmd_resume))
        app.add_handler(CommandHandler("mode", self.cmd_mode))
        app.add_handler(CommandHandler("risk", self.cmd_risk))
        app.add_handler(CommandHandler("rr", self.cmd_rr))
        app.add_handler(CommandHandler("score", self.cmd_score))
        app.add_handler(CallbackQueryHandler(self.handle_callback))
        self.app = app
