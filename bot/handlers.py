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
/mode [demo|live] — Switch execution mode
/risk [pct]     — Set risk per trade
/rr [ratio]     — Set minimum RR ratio
/score [val]    — Set score threshold
/help           — Show all commands
"""

from __future__ import annotations

import logging
import json
import asyncio
from typing import Optional

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    CommandHandler, CallbackQueryHandler, MessageHandler,
    ContextTypes, filters, Application,
)

from config import TradeSettings, get_admin_ids, get_mt5_credentials
from bot import keyboards
from storage import db
from analysis.scoring import format_signal_report, TradeSignal
from risk.manager import RiskManager
from executors.mt5 import MT5Executor

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

    async def reconnect_executor(self, mode: str) -> bool:
        """Disconnect current MT5 session and reconnect to the requested mode."""
        try:
            # Disconnect current
            if hasattr(self.executor, 'disconnect'):
                await self.executor.disconnect()
            
            # Get new credentials
            creds = get_mt5_credentials(mode)
            if not creds["login"] or not creds["password"] or not creds["server"]:
                logger.error(f"Missing credentials for {mode}")
                return False
            
            # Update executor attributes
            self.executor.login = creds["login"]
            self.executor.password = creds["password"]
            self.executor.server = creds["server"]
            self.executor.path = creds["path"]
            
            # Connect
            return await self.executor.connect()
        except Exception as e:
            logger.error(f"Reconnection error: {e}")
            return False

    # ─── Commands ──────────────────────────────────────────

    @admin_only
    async def cmd_start(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Show the main menu."""
        await self.reload_settings()
        text = (
            f"🤖 **SMC Trading Bot**\n\n"
            f"Mode: `{self.settings.trading_mode.upper()}`\n"
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
    async def cmd_help(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Show help message."""
        help_text = (
            "🤖 **SMC Trading Bot Commands**\n\n"
            "/start - Show main menu\n"
            "/scan - Scan all symbols for signals\n"
            "/analyze [symbol] - Deep analysis of a symbol\n"
            "/positions - Show open positions\n"
            "/close_all - Close all open positions\n"
            "/settings - Adjust bot settings\n"
            "/account - Show MT5 account info\n"
            "/debug_mt5 - Run MT5 health & permission check\n"
            "/history - Show recent trade history\n"
            "/pause - Pause auto-trading\n"
            "/resume - Resume auto-trading\n"
            "/mode [demo|live] - Switch execution mode\n"
            "/risk [pct] - Set risk per trade (e.g. /risk 1.0)\n"
            "/rr [ratio] - Set min RR ratio (e.g. /rr 3.0)\n"
            "/score [val] - Set score threshold (e.g. /score 60)\n"
            "/backtest [symbol] [tf] [days] - Run a backtest\n"
            "/sessions - Check trading session status\n"
            "/news - Check news filter status"
        )
        await update.message.reply_text(help_text)

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
            f"Mode: {self.settings.trading_mode.upper()}\n"
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
            f"💰 **Account Info ({self.settings.trading_mode.upper()})**\n\n"
            f"Login: {info.get('login', 'N/A')}\n"
            f"Balance: ${info.get('balance', 0):.2f}\n"
            f"Equity: ${info.get('equity', 0):.2f}\n"
            f"Free Margin: ${info.get('free_margin', 0):.2f}\n"
            f"Used Margin: ${info.get('margin', 0):.2f}\n"
            f"Leverage: 1:{info.get('leverage', 0)}\n"
            f"Currency: {info.get('currency', 'USD')}\n"
            f"Server: {info.get('server', 'N/A')}"
        )

    @admin_only
    async def cmd_debug_mt5(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Deep health check for MT5 connection and permissions."""
        if not hasattr(self.executor, 'get_diagnostic_info'):
            await update.message.reply_text("Diagnostic tool not supported by current executor.")
            return

        await update.message.reply_text("🔍 Running MT5 diagnostic health check...")
        diag = await self.executor.get_diagnostic_info()

        if not diag.get("available"):
            await update.message.reply_text(f"❌ MT5 Package Error: {diag.get('error')}")
            return

        lines = [
            "🛠 **MT5 Diagnostic Report**\n",
            f"**Terminal Status:** {'✅ Running' if diag.get('terminal_running') else '❌ Not Found'}",
            f"**Connection:** {'✅ Connected' if diag.get('connected_to_server') else '❌ Disconnected'}",
            f"**Algo Trading (Global):** {'✅ ENABLED' if diag.get('trade_expert') else '❌ DISABLED (Check Green Button)'}",
            f"**DLL Imports:** {'✅ Allowed' if diag.get('dll_allowed') else '❌ Blocked'}",
            f"**Account Trading:** {'✅ Allowed' if diag.get('trade_allowed_acc') else '❌ Restricted (Check Password)'}",
            f"**Expert Trading:** {'✅ Allowed' if diag.get('trade_expert_acc') else '❌ Restricted'}",
            f"\n**Session Info:**",
            f"Login: `{diag.get('login', 'N/A')}`",
            f"Server: `{diag.get('server', 'N/A')}`",
            f"Build: `{diag.get('build', 'N/A')}`",
            f"\n**Last MT5 Error:** `{diag.get('last_error', 'None')}`"
        ]

        if not diag.get('trade_expert'):
            lines.append("\n⚠️ *Action Required: Click the 'Algo Trading' button in MT5 to turn it GREEN.*")
        if not diag.get('trade_allowed_acc'):
            lines.append("\n⚠️ *Action Required: Ensure you are logged in with the MASTER password, not Investor.*")

        await update.message.reply_text("\n".join(lines))

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
    async def cmd_set_balance(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Set a virtual balance for risk calculations."""
        if not context.args:
            await update.message.reply_text("Usage: /set_balance [amount] (e.g. /set_balance 500) or /set_balance reset")
            return

        arg = context.args[0].lower()
        if arg == "reset":
            self.settings.virtual_balance = None
            await db.save_settings(self.settings)
            await update.message.reply_text("✅ Virtual balance reset. Using real MT5 balance for risk.")
        else:
            try:
                val = float(arg)
                if val <= 0:
                    raise ValueError
                self.settings.virtual_balance = val
                await db.save_settings(self.settings)
                await update.message.reply_text(f"✅ Virtual balance set to **${val:,.2f}**. Risk will be calculated from this amount.")
            except ValueError:
                await update.message.reply_text("❌ Invalid amount. Please enter a positive number.")

    @admin_only
    async def cmd_burn_to(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Burn demo balance down to a target amount via high-lot trades."""
        if self.settings.trading_mode != "demo":
            await update.message.reply_text("⛔ The /burn_to command is only allowed in DEMO mode for safety.")
            return

        if not context.args:
            await update.message.reply_text("Usage: /burn_to [target_amount] (e.g. /burn_to 500)")
            return

        try:
            target = float(context.args[0])
            account = await self.executor.get_account_info()
            current_balance = account.get("balance", 0)

            if target >= current_balance:
                await update.message.reply_text(f"Target ${target:,.2f} must be lower than current balance ${current_balance:,.2f}")
                return

            await update.message.reply_text(
                f"🔥 **BALANCE BURNER ACTIVATED**\n"
                f"Current: ${current_balance:,.2f}\n"
                f"Target: ${target:,.2f}\n\n"
                f"I will now open high-lot trades to reduce the balance. Please wait..."
            )

            # Start the burn process in the background
            asyncio.create_task(self._run_balance_burn(target))

        except ValueError:
            await update.message.reply_text("❌ Invalid amount. Please enter a number.")

    async def _run_balance_burn(self, target: float):
        """Background task to open and close trades until target is reached."""
        try:
            while True:
                account = await self.executor.get_account_info()
                current_equity = account.get("equity", 0)
                
                if current_equity <= target:
                    await self.executor.close_all_positions()
                    if self.app and self.admin_chat_id:
                        await self.app.bot.send_message(
                            get_admin_ids()[0], 
                            f"✅ **BURN COMPLETE**\nTarget reached: ${current_equity:,.2f}\nAll positions closed."
                        )
                    break

                # Sort symbols to prioritize Indices for faster burning
                sorted_symbols = sorted(self.settings.symbols, key=lambda x: "Index" in x or "Volatility" in x, reverse=True)
                
                # BATCH OPENING: Open hedged trades on multiple symbols at once
                active_symbols = []
                for sym in sorted_symbols[:5]: # Try top 5 indices at once
                    sym_info = await self.executor.get_symbol_info(sym)
                    if not sym_info or not sym_info.get("max_lot"): continue
                    
                    # Determine safe lot size (start with max, scale down if needed)
                    test_lot = sym_info.get("max_lot")
                    res = await self.executor.execute_trade(
                        symbol=sym, direction="BUY", lot_size=test_lot,
                        sl=0, tp=0, magic=999999, comment="BURN"
                    )
                    
                    # If max lot fails, try a smaller safer lot
                    if not res.success:
                        test_lot = round(test_lot * 0.1, 2) # Try 10% of max
                        res = await self.executor.execute_trade(
                            symbol=sym, direction="BUY", lot_size=test_lot,
                            sl=0, tp=0, magic=999999, comment="BURN"
                        )
                    
                    if res.success:
                        # Hedge it
                        await self.executor.execute_trade(
                            symbol=sym, direction="SELL", lot_size=test_lot,
                            sl=0, tp=0, magic=999999, comment="BURN"
                        )
                        active_symbols.append(sym)
                        logger.info(f"HyperBurner: Opened hedge on {sym} ({test_lot} lots)")

                if not active_symbols:
                    logger.error("HyperBurner: No symbols available. Retrying in 5s...")
                    await asyncio.sleep(5)
                    continue

                # Wait briefly to let spread/slippage burn equity
                await asyncio.sleep(4)
                
                # BATCH CLOSING: Realize the losses and clear margin
                closed_count = await self.executor.close_all_positions()
                logger.info(f"HyperBurner: Cycle complete. Closed {closed_count} positions. Equity: ${current_equity:,.2f}")
                
                await asyncio.sleep(1) # Brief pause before next cycle
                
        except Exception as e:
            logger.error(f"Burn process error: {e}")

    @admin_only
    async def cmd_mode(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Switch execution mode."""
        if context.args and context.args[0].lower() in ("demo", "live"):
            mode = context.args[0].lower()
            if mode == "live":
                await update.message.reply_text(
                    "⚠️ Switching to LIVE mode will execute real trades.\n"
                    "Are you sure?",
                    reply_markup=keyboards.confirm_keyboard("mode_live")
                )
            else:
                # Switching to demo
                success = await self.reconnect_executor("demo")
                if success:
                    self.settings.trading_mode = "demo"
                    await db.save_settings(self.settings)
                    await update.message.reply_text("✅ Switched to DEMO mode.")
                else:
                    await update.message.reply_text("❌ Failed to connect to Demo account. Check .env credentials.")
        else:
            await update.message.reply_text(
                f"Current mode: {self.settings.trading_mode.upper()}\nUsage: /mode demo or /mode live"
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
                await update.message.reply_text("Usage: /score 60")
        else:
            await update.message.reply_text(f"Current threshold: {self.settings.score_threshold}%\nUsage: /score 60")

    @admin_only
    async def cmd_backtest(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Run a backtest."""
        if not context.args:
            await update.message.reply_text("Usage: /backtest EURUSD H1 180")
            return
            
        symbol = context.args[0]
        timeframe = context.args[1] if len(context.args) > 1 else "H1"
        try:
            days = int(context.args[2]) if len(context.args) > 2 else 180
        except ValueError:
            days = 180

        await update.message.reply_text(
            f"📊 Running backtest: {symbol} {timeframe} over {days} days...\n"
            f"This may take a moment."
        )

        try:
            from backtest.runner import run_backtest
            await self.reload_settings()
            result = await run_backtest(symbol, timeframe, days, self.settings)
            await update.message.reply_text(result.summary())
        except Exception as e:
            logger.error(f"Backtest error: {e}", exc_info=True)
            await update.message.reply_text(f"❌ Backtest failed: {e}")

    @admin_only
    async def cmd_sessions(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Show trading session status."""
        from analysis.sessions import format_session_status
        await update.message.reply_text(format_session_status(self.settings.enabled_sessions))

    @admin_only
    async def cmd_news(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Check news filter status for a symbol."""
        symbol = context.args[0] if context.args else self.settings.symbols[0]
        if self.scheduler and hasattr(self.scheduler, 'news_filter') and self.scheduler.news_filter:
            result = await self.scheduler.news_filter.check_news(symbol)
            status = "🔴 BLOCKED" if result.is_blackout else "🟢 CLEAR"
            await update.message.reply_text(
                f"📰 **News Filter: {symbol}**\n\n"
                f"Status: {status}\n"
                f"Reason: {result.reason}"
            )
        else:
            await update.message.reply_text("News filter not initialized.")

    # ─── Callbacks ──────────────────────────────────────────

    async def handle_callback(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle inline keyboard callbacks."""
        query = update.callback_query
        await query.answer()
        data = query.data

        if data == "main":
            await self.cmd_start(update, context)
            await query.message.delete()
        elif data == "scan":
            await self.cmd_scan(update, context)
        elif data == "positions":
            await self.cmd_positions(update, context)
        elif data == "settings":
            await self.cmd_settings(update, context)
        elif data == "account":
            await self.cmd_account(update, context)
        elif data == "history":
            await self.cmd_history(update, context)
        elif data == "analyze":
            await self.cmd_analyze(update, context)
        elif data == "pause":
            await self.cmd_pause(update, context)
        elif data == "resume":
            await self.cmd_resume(update, context)
        elif data == "close_all":
            await self.cmd_close_all(update, context)
        elif data == "confirm_close_all":
            closed = await self.executor.close_all_positions()
            await query.edit_message_text(f"✅ Closed {closed} positions.")
        elif data == "set_autotrade":
            await query.edit_message_text(
                f"Auto-Trade is currently {'ON ✅' if self.settings.auto_trade else 'OFF ❌'}",
                reply_markup=keyboards.autotrade_menu(self.settings.auto_trade)
            )
        elif data == "toggle_autotrade":
            if not self.settings.auto_trade:
                await query.edit_message_text(
                    "⚠️ Enable Auto-Trade? The bot will execute trades automatically.",
                    reply_markup=keyboards.confirm_keyboard("autotrade_on")
                )
            else:
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
                f"Current mode: {self.settings.trading_mode.upper()}",
                reply_markup=keyboards.mode_menu(self.settings.trading_mode)
            )
        elif data == "mode_demo":
            success = await self.reconnect_executor("demo")
            if success:
                self.settings.trading_mode = "demo"
                await db.save_settings(self.settings)
                await query.edit_message_text("✅ Switched to DEMO mode.", reply_markup=keyboards.settings_menu())
            else:
                await query.edit_message_text("❌ Failed to connect to Demo account. Check .env.", reply_markup=keyboards.settings_menu())
        elif data == "mode_live":
            await query.edit_message_text(
                "⚠️ Switch to LIVE mode? Real trades will execute with real money.",
                reply_markup=keyboards.confirm_keyboard("mode_live")
            )
        elif data == "confirm_mode_live":
            success = await self.reconnect_executor("live")
            if success:
                self.settings.trading_mode = "live"
                await db.save_settings(self.settings)
                await query.edit_message_text("⚠️ LIVE mode enabled. Real trades will execute.", reply_markup=keyboards.settings_menu())
            else:
                await query.edit_message_text("❌ Failed to connect to Live account. Check .env.", reply_markup=keyboards.settings_menu())
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
                f"Current score threshold: {self.settings.score_threshold}%\nUse command: /score 60\n(Range: 1 - 100)",
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
        app.add_handler(CommandHandler("debug_mt5", self.cmd_debug_mt5))
        app.add_handler(CommandHandler("history", self.cmd_history))
        app.add_handler(CommandHandler("pause", self.cmd_pause))
        app.add_handler(CommandHandler("resume", self.cmd_resume))
        app.add_handler(CommandHandler("set_balance", self.cmd_set_balance))
        app.add_handler(CommandHandler("burn_to", self.cmd_burn_to))
        app.add_handler(CommandHandler("mode", self.cmd_mode))
        app.add_handler(CommandHandler("risk", self.cmd_risk))
        app.add_handler(CommandHandler("rr", self.cmd_rr))
        app.add_handler(CommandHandler("score", self.cmd_score))
        app.add_handler(CommandHandler("backtest", self.cmd_backtest))
        app.add_handler(CommandHandler("sessions", self.cmd_sessions))
        app.add_handler(CommandHandler("news", self.cmd_news))
        app.add_handler(CallbackQueryHandler(self.handle_callback))
        self.app = app
