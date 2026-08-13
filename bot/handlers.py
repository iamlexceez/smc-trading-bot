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
from analysis.scoring import format_signal_report
from analysis.profiler import profiler
from analysis.order_flow import order_flow
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
        self._executor = executor
        self.risk_manager = risk_manager
        self.scheduler = scheduler
        self.app: Optional[Application] = None

    @property
    def executor(self):
        """Always return the active executor from the scheduler if available."""
        if self.scheduler and hasattr(self.scheduler, 'executor'):
            return self.scheduler.executor
        return self._executor

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
            f"Paused: {'⏸ YES' if self.settings.is_paused else '▶️ NO'}\n"
            f"Aggressive: {'🔥 YES' if self.settings.aggressive_mode else '🛡 NO'}\n\n"
            f"Risk/trade: {self.settings.risk_per_trade}%\n"
            f"Min RR: 1:{self.settings.min_rr_ratio}\n"
            f"Score threshold: {self.settings.score_threshold}%\n\n"
            f"Enabled Symbols: {', '.join(self.settings.enabled_symbols[:5])}{'...' if len(self.settings.enabled_symbols) > 5 else ''}\n"
            f"Timeframes: {', '.join(self.settings.timeframes)}\n\n"
            f"Choose an option below 👇"
        )
        if update.callback_query:
            await update.callback_query.edit_message_text(text, reply_markup=keyboards.main_menu())
        else:
            await update.message.reply_text(text, reply_markup=keyboards.main_menu())

    @admin_only
    async def cmd_help(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Show help message."""
        help_text = (
            "🤖 **SMC Trading Bot Commands**\n\n"
            "**Core Controls:**\n"
            "/start - Show the main menu\n"
            "/scan - Scan enabled symbols using hard validity gates\n"
            "/positions - Show live positions and recorded bot actions\n"
            "/baskets - Show setup risk budgets and planned layers\n"
            "/manage [ticket] - Review one position and safely optimize its SL/TP\n"
            "/safety - Show active limits and circuit breakers\n"
            "/account - Show MT5 account information\n"
            "/settings - Open the settings dashboard\n"
            "/debug_mt5 - Run the MT5 connection and permission check\n\n"
            "**Setup & Risk:**\n"
            "/entry_mode [confirmed|aggressive|extreme confirm] - Select the confirmation model\n"
            "/risk [pct] - Set risk per setup within the hard 1% cap\n"
            "/rr [ratio] - Set the minimum market-derived RR\n"
            "/score [pct] - Set the minimum setup-quality threshold\n"
            "/daily_limit [pct] - Set the daily profit stop\n"
            "/cooldown [min] - Set the symbol cooldown\n\n"
            "**Monitoring & Analysis:**\n"
            "/journal - View the daily learning journal\n"
            "/optimize - Run the performance optimizer\n"
            "/profile [symbol] - View Symbol DNA\n"
            "/history - Show recent trade history\n"
            "/backtest [symbol] [tf] [days] - Run a backtest\n"
            "/news - Check the news filter\n"
            "/sessions - Check trading-session status"
        )
        await update.message.reply_text(help_text)

    @admin_only
    async def cmd_sessions_all(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Enable all trading sessions."""
        self.settings.enabled_sessions = ["all"]
        await db.save_settings(self.settings)
        await update.message.reply_text("🌍 **ALL SESSIONS ENABLED**\nThe bot will now trade 24/5.")

    @admin_only
    async def cmd_profile(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Show the behavioral profile for a symbol."""
        if not context.args:
            await update.message.reply_text("Usage: `/profile [symbol]` (e.g., `/profile EURUSD`)")
            return

        symbol = " ".join(context.args).strip()
        profile = profiler.profiles.get(symbol)
        
        if not profile:
            await update.message.reply_text(f"No profile data for {symbol} yet. Run a `/scan` first.")
            return

        # Fetch latest Order Flow profile
        of_text = ""
        if self.scheduler:
            df = await self.scheduler.fetch_candles(symbol, profile.best_timeframe, 200)
            of_profile = order_flow.calculate_profile(df)
            if of_profile:
                of_text = (
                    f"🎯 **Point of Control (POC)**: `{of_profile.poc}`\n"
                    f"📦 **Value Area**: `{of_profile.value_area_low} - {of_profile.value_area_high}`\n"
                    f"⚡️ **Volume Intensity**: `{of_profile.delta_intensity}x`\n\n"
                )

        text = (
            f"🧬 **Symbol DNA: {symbol}**\n\n"
            f"{of_text}"
            f"📊 **Volatility Index**: `{profile.volatility_index}/100`\n"
            f"🏛 **Structure Respect**: `{profile.structure_respect_score}%`\n"
            f"🎯 **Optimal ATR Mult**: `{profile.optimal_atr_multiplier}x`\n"
            f"⏱ **Best Timeframe**: `{profile.best_timeframe}`\n"
            f"🏆 **Historical Win Rate (OB)**: `{profile.historical_win_rate_ob}%`\n"
            f"🏆 **Historical Win Rate (FVG)**: `{profile.historical_win_rate_fvg}%`\n\n"
            f"_Last learned: {profile.last_updated.strftime('%Y-%m-%d %H:%M')}_"
        )
        await update.message.reply_text(text)

    @admin_only
    async def cmd_scan(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Trigger a market scan and auto-execute valid signals."""
        reply_target = update.callback_query.message if update.callback_query else update.message
        if self.scheduler:
            await reply_target.reply_text("🔍 **MANUAL SCAN & EXECUTION INITIATED**\n_Scanning for high-probability setups..._")
            
            # Use the existing scan logic
            signals = []
            for symbol in self.scheduler.settings.enabled_symbols:
                try:
                    # 1. Fetch data
                    primary_tf = "M1" if self.scheduler.settings.aggressive_mode else "M15"
                    df = await self.scheduler.fetch_candles(symbol, primary_tf, 500)
                    if df.empty: continue
                    
                    # 2. Analyze
                    signal = await self.scheduler.analyze_symbol(symbol)
                    if not signal or not signal.passed or signal.score < self.scheduler.settings.min_setup_score:
                        continue
                    
                    # 3. Auto-Execute
                    await self.scheduler.execute_signal(signal, df)
                    signals.append(signal)
                    
                except Exception as e:
                    logger.error(f"Error in manual scan for {symbol}: {e}")

            if not signals:
                await reply_target.reply_text("No tradeable setups found at this time.")
        else:
            await reply_target.reply_text("Scheduler not initialized.")

    @admin_only
    async def cmd_analyze(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Show analysis menu or analyze a specific symbol."""
        reply_target = update.callback_query.message if update.callback_query else update.message
        if context.args:
            symbol = " ".join(context.args)
            await self._do_analysis(update, symbol)
        else:
            await reply_target.reply_text(
                "Select a symbol to analyze:",
                reply_markup=keyboards.analysis_menu(self.settings.symbols)
            )

    async def _do_analysis(self, update: Update, symbol: str):
        """Perform deep analysis on a symbol."""
        reply_target = update.callback_query.message if update.callback_query else update.message
        if self.scheduler:
            await reply_target.reply_text(f"📊 Analyzing {symbol}...")
            signal = await self.scheduler.analyze_symbol(symbol)
            if signal:
                await reply_target.reply_text(format_signal_report(signal), parse_mode="Markdown")
            else:
                await reply_target.reply_text(f"No tradeable signal for {symbol} at this time.")
        else:
            await reply_target.reply_text("Scheduler not initialized.")

    @admin_only
    async def cmd_positions(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Show live positions with basket state and recent bot management actions."""
        reply_target = update.callback_query.message if update.callback_query else update.message
        positions = await self.executor.get_open_positions()
        if not positions:
            await reply_target.reply_text("No open positions.")
            return

        total_profit = 0.0
        for position in positions:
            emoji = "🟢" if position.profit >= 0 else "🔴"
            basket = await db.get_basket_for_ticket(position.ticket)
            logs = await db.get_trade_logs(ticket=position.ticket)
            if basket:
                layers = await db.get_basket_layers(basket["id"])
                open_layers = sum(1 for layer in layers if layer["status"] == "open")
                planned_layers = sum(1 for layer in layers if layer["status"] == "planned")
                basket_text = (
                    f"\n📦 Basket `#{basket['id']}` — `{basket['state']}`\n"
                    f"Risk: `${basket['reserved_risk']:.2f}` / `${basket['max_risk']:.2f}` | "
                    f"Layers: `{open_layers}` open, `{planned_layers}` planned"
                )
            else:
                basket_text = "\n📦 *Manual/untracked MT5 position — no automated layers will be added.*"
            if logs:
                actions = []
                for log in logs[-3:]:
                    detail = str(log["details"])
                    actions.append(f"• **{log['action']}** — {detail[:160]}")
                log_text = "\n🛠 **Recent actions:**\n" + "\n".join(actions)
            else:
                log_text = "\n*No bot management action recorded yet.*"

            text = (
                f"{emoji} **#{position.ticket} | {position.direction} {position.volume} {position.symbol}**\n"
                f"Entry: `{position.entry_price:.5f}` | SL: `{position.sl:.5f}` | TP: `{position.tp:.5f}`\n"
                f"PnL: **${position.profit:.2f}**"
                f"{basket_text}{log_text}"
            )
            keyboard = [[InlineKeyboardButton("🎯 Review & Optimize", callback_data=f"manage_{position.ticket}")]]
            await reply_target.reply_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")
            total_profit += position.profit

        await reply_target.reply_text(f"💰 **Total Open PnL: ${total_profit:.2f}**", parse_mode="Markdown")

    @admin_only
    async def cmd_manage(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Manually trigger position management."""
        if not context.args:
            await update.message.reply_text("Usage: `/manage [ticket_number]`")
            return
        
        try:
            ticket = int(context.args[0])
            if self.scheduler:
                msg = await self.scheduler.manual_manage_position(ticket)
                await update.message.reply_text(msg, parse_mode="Markdown")
            else:
                await update.message.reply_text("❌ Scheduler not initialized.")
        except ValueError:
            await update.message.reply_text("❌ Invalid ticket number.")

    @admin_only
    async def cmd_close_all(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Close all positions with confirmation."""
        positions = await self.executor.get_open_positions()
        if not positions:
            msg = "No open positions to close."
            if update.callback_query:
                await update.callback_query.edit_message_text(msg)
            else:
                await update.message.reply_text(msg)
            return
        text = f"⚠️ Close all {len(positions)} open positions?\nThis action cannot be undone."
        if update.callback_query:
            await update.callback_query.edit_message_text(text, reply_markup=keyboards.confirm_keyboard("close_all"))
        else:
            await update.message.reply_text(text, reply_markup=keyboards.confirm_keyboard("close_all"))

    @admin_only
    async def cmd_settings(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Show settings menu."""
        await self.reload_settings()
        text = self._format_settings()
        if update.callback_query:
            await update.callback_query.edit_message_text(text, reply_markup=keyboards.settings_menu(), parse_mode="Markdown")
        else:
            await update.message.reply_text(text, reply_markup=keyboards.settings_menu(), parse_mode="Markdown")

    def _format_settings(self) -> str:
        return (
            "⚙️ **Current Settings**\n\n"
            f"Entry mode: {self.settings.entry_mode.upper()}\n"
            f"Risk/setup: {self.settings.risk_per_trade}% (cap {self.settings.max_setup_risk_pct}%)\n"
            f"Open-risk ceiling: {self.settings.max_total_open_risk_pct}%\n"
            f"Daily loss / profit stop: -{self.settings.max_daily_loss_pct}% / +{self.settings.daily_profit_stop_pct}%\n"
            f"Max trades/day: {self.settings.max_trades_per_day}\n"
            f"Max positions / layers: {self.settings.max_open_positions} / {self.settings.max_layers}\n"
            f"Min RR: 1:{self.settings.min_rr_ratio}\n"
            f"Minimum setup quality: {self.settings.min_setup_score}%\n"
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
            msg = "Could not retrieve account info."
            if update.callback_query:
                await update.callback_query.edit_message_text(msg)
            else:
                await update.message.reply_text(msg)
            return
        text = (
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
        if update.callback_query:
            await update.callback_query.edit_message_text(text, reply_markup=keyboards.main_menu(), parse_mode="Markdown")
        else:
            await update.message.reply_text(text, reply_markup=keyboards.main_menu(), parse_mode="Markdown")

    @admin_only
    async def cmd_debug_mt5(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Deep health check for MT5 connection and permissions."""
        if not hasattr(self.executor, 'get_diagnostic_info'):
            msg = "Diagnostic tool not supported by current executor."
            if update.callback_query:
                await update.callback_query.edit_message_text(msg)
            else:
                await update.message.reply_text(msg)
            return

        msg = "🔍 Running MT5 diagnostic health check..."
        if update.callback_query:
            await update.callback_query.edit_message_text(msg)
        else:
            await update.message.reply_text(msg)
            
        diag = await self.executor.get_diagnostic_info()

        if not diag.get("available"):
            err_msg = f"❌ MT5 Package Error: {diag.get('error')}"
            if update.callback_query:
                await update.callback_query.edit_message_text(err_msg)
            else:
                await update.message.reply_text(err_msg)
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

        text = "\n".join(lines)
        if update.callback_query:
            await update.callback_query.edit_message_text(text, reply_markup=keyboards.main_menu(), parse_mode="Markdown")
        else:
            await update.message.reply_text(text, reply_markup=keyboards.main_menu(), parse_mode="Markdown")

    @admin_only
    async def cmd_history(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Show recent trade history."""
        trades = await db.get_trade_history(limit=15)
        if not trades:
            msg = "No trade history yet."
            if update.callback_query:
                await update.callback_query.edit_message_text(msg, reply_markup=keyboards.main_menu())
            else:
                await update.message.reply_text(msg, reply_markup=keyboards.main_menu())
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
        text = "\n".join(lines)
        if update.callback_query:
            await update.callback_query.edit_message_text(text, reply_markup=keyboards.main_menu(), parse_mode="Markdown")
        else:
            await update.message.reply_text(text, reply_markup=keyboards.main_menu(), parse_mode="Markdown")

    @admin_only
    async def cmd_pause(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Pause auto-trading."""
        self.settings.is_paused = True
        await db.save_settings(self.settings)
        msg = "⏸ Auto-trading paused. Use /resume to continue."
        if update.callback_query:
            await update.callback_query.edit_message_text(msg, reply_markup=keyboards.main_menu())
        else:
            await update.message.reply_text(msg, reply_markup=keyboards.main_menu())

    @admin_only
    async def cmd_resume(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Resume auto-trading."""
        self.settings.is_paused = False
        await db.save_settings(self.settings)
        msg = "▶️ Auto-trading resumed."
        if update.callback_query:
            await update.callback_query.edit_message_text(msg, reply_markup=keyboards.main_menu())
        else:
            await update.message.reply_text(msg, reply_markup=keyboards.main_menu())

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
    async def cmd_aggressive(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Toggle Aggressive Growth mode."""
        if not context.args:
            status = "ON" if self.settings.aggressive_mode else "OFF"
            await update.message.reply_text(f"🚀 Aggressive Growth is currently **{status}**.\nUse `/aggressive on` or `/aggressive off` to toggle.")
            return

        arg = context.args[0].lower()
        if arg == "on":
            self.settings.aggressive_mode = True
            await db.save_settings(self.settings)
            await update.message.reply_text("🚀 **AGGRESSIVE GROWTH ACTIVATED**\nRisk per trade will be increased and the bot will hunt for more setups.")
        elif arg == "off":
            self.settings.aggressive_mode = False
            await db.save_settings(self.settings)
            await update.message.reply_text("🛡 **AGGRESSIVE GROWTH DEACTIVATED**\nReturning to standard SMC safety protocols.")
        else:
            await update.message.reply_text("Usage: `/aggressive on` or `/aggressive off`")

    @admin_only
    async def cmd_toggle_symbol(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Toggle a symbol on or off without removing it."""
        if not context.args:
            lines = ["🔄 **Symbol Toggles**\n"]
            for s in self.settings.symbols:
                status = "✅ ENABLED" if s in self.settings.enabled_symbols else "❌ DISABLED"
                lines.append(f"{s}: {status}")
            lines.append("\nUse `/toggle_symbol [name]` to switch.")
            await update.message.reply_text("\n".join(lines))
            return

        symbol = " ".join(context.args).strip()
        # Case-insensitive search
        match = next((s for s in self.settings.symbols if s.lower() == symbol.lower()), None)
        
        if not match:
            await update.message.reply_text(f"Symbol '{symbol}' not found in available list. Use `/add_symbol` first.")
            return

        if match in self.settings.enabled_symbols:
            self.settings.enabled_symbols.remove(match)
            await update.message.reply_text(f"❌ **{match} DISABLED**\nThe bot will no longer scan this pair.")
        else:
            self.settings.enabled_symbols.append(match)
            await update.message.reply_text(f"✅ **{match} ENABLED**\nThe bot will now include this pair in scans.")
        
        await db.save_settings(self.settings)

    @admin_only
    async def cmd_expert_mode(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Activate the Expert Selection of symbols."""
        expert_selection = [
            "Volatility 75 Index", "Volatility 100 Index", "Volatility 10 Index", "Volatility 25 Index",
            "EURUSD", "GBPUSD", "USDJPY", "XAUUSD"
        ]
        
        # Add any expert symbols that aren't in the available list yet
        for s in expert_selection:
            if s not in self.settings.symbols:
                self.settings.symbols.append(s)
        
        # Set enabled symbols to exactly the expert selection
        self.settings.enabled_symbols = [s for s in expert_selection]
        
        await db.save_settings(self.settings)
        await update.message.reply_text(
            "🏆 **EXPERT MODE ACTIVATED**\n\n"
            "The bot is now focused on high-probability institutional pairs:\n"
            "- Volatility 75, 100, 10, 25\n"
            "- EURUSD, GBPUSD, USDJPY, XAUUSD\n\n"
            "Your previous symbols are still in the list but have been **DISABLED**."
        )

    @admin_only
    async def cmd_focus_indices(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Toggle Index Focus Mode."""
        if not context.args:
            status = "ON" if self.settings.index_focus else "OFF"
            await update.message.reply_text(f"📊 Index Focus Mode is currently **{status}**.\nUse `/focus_indices on` or `/focus_indices off` to toggle.")
            return

        arg = context.args[0].lower()
        if arg == "on":
            self.settings.index_focus = True
            # Move all Volatility indices to the front of enabled_symbols
            indices = [s for s in self.settings.enabled_symbols if "Volatility" in s]
            others = [s for s in self.settings.enabled_symbols if "Volatility" not in s]
            self.settings.enabled_symbols = indices + others
            
            await db.save_settings(self.settings)
            await update.message.reply_text(
                "🎯 **INDEX FOCUS ACTIVATED**\n\n"
                "The bot will now prioritize **Volatility Indices** for scans and execution.\n"
                "This is recommended for smaller accounts ($200) to pass margin checks."
            )
        elif arg == "off":
            self.settings.index_focus = False
            await db.save_settings(self.settings)
            await update.message.reply_text("🛡 **INDEX FOCUS DEACTIVATED**\nReturning to standard symbol priority.")
        else:
            await update.message.reply_text("Usage: `/focus_indices on` or `/focus_indices off`")

    @admin_only
    async def cmd_scalping(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Toggle Scalping Mode (M1/M5 timeframes)."""
        if not context.args:
            is_scalping = "M1" in self.settings.timeframes
            status = "ON" if is_scalping else "OFF"
            await update.message.reply_text(f"⏱ Scalping Mode is currently **{status}**.\nUse `/scalping on` or `/scalping off` to toggle.")
            return

        arg = context.args[0].lower()
        if arg == "on":
            # Add M1, M5 to the front of timeframes
            new_tfs = ["M1", "M5"]
            for tf in self.settings.timeframes:
                if tf not in new_tfs:
                    new_tfs.append(tf)
            self.settings.timeframes = new_tfs
            await db.save_settings(self.settings)
            await update.message.reply_text("⏱ **SCALPING MODE ACTIVATED**\nThe bot is now hunting for snipers on M1 and M5 timeframes.")
        elif arg == "off":
            # Remove M1, M5
            self.settings.timeframes = [tf for tf in self.settings.timeframes if tf not in ["M1", "M5"]]
            if not self.settings.timeframes:
                self.settings.timeframes = ["M15", "H1", "H4"]
            await db.save_settings(self.settings)
            await update.message.reply_text("🛡 **SCALPING MODE DEACTIVATED**\nReturning to standard M15+ timeframes.")
        else:
            await update.message.reply_text("Usage: `/scalping on` or `/scalping off`")

    @admin_only
    async def cmd_target(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Set a balance target for the current cycle."""
        if not context.args:
            target = f"${self.settings.target_balance:,.2f}" if self.settings.target_balance else "None"
            await update.message.reply_text(f"🎯 Current Cycle Target: **{target}**\nUse `/target [amount]` or `/target reset`.")
            return

        arg = context.args[0].lower()
        if arg == "reset":
            self.settings.target_balance = None
            await db.save_settings(self.settings)
            await update.message.reply_text("🎯 Cycle target reset. The bot will trade indefinitely.")
        else:
            try:
                val = float(arg)
                if val <= 0: raise ValueError
                self.settings.target_balance = val
                await db.save_settings(self.settings)
                await update.message.reply_text(f"🎯 **CYCLE TARGET SET: ${val:,.2f}**\nThe bot will close all positions and stop trading once this balance is reached.")
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
                    
                    # Determine safe lot size using normalization
                    test_lot = self.risk_manager.normalize_lot(sym_info.get("max_lot"), sym_info)
                    res = await self.executor.execute_trade(
                        symbol=sym, direction="BUY", lot_size=test_lot,
                        sl=0, tp=0, magic=999999, comment="BURN"
                    )
                    
                    # If max lot fails, try a smaller safer lot
                    if not res.success:
                        test_lot = self.risk_manager.normalize_lot(test_lot * 0.1, sym_info)
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
                self.settings.risk_per_trade = max(0.1, min(val, self.settings.max_setup_risk_pct, 1.0))
                await db.save_settings(self.settings)
                await update.message.reply_text(f"✅ Risk per setup set to {self.settings.risk_per_trade}% (hard capped at {min(self.settings.max_setup_risk_pct, 1.0)}%).")
            except ValueError:
                await update.message.reply_text("Usage: /risk 1.0")
        else:
            await update.message.reply_text(f"Current risk per setup: {self.settings.risk_per_trade}% (hard cap: {min(self.settings.max_setup_risk_pct, 1.0)}%)\nUsage: /risk 0.75")

    @admin_only
    async def cmd_entry_mode(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Set the required entry-confirmation mode without changing risk caps."""
        if not context.args:
            await update.message.reply_text(
                f"**Entry Mode:** `{self.settings.entry_mode}`\n\n"
                "`/entry_mode confirmed` — requires LTF confirmation after the full setup chain.\n"
                "`/entry_mode aggressive` — earlier entry after sweep, displacement, BOS/CHOCH, zone, and real target.\n"
                "`/entry_mode extreme confirm` — only enabled when extreme mode is explicitly authorized and setup quality is at least 90."
            )
            return
        mode = context.args[0].lower()
        if mode not in {"confirmed", "aggressive", "extreme"}:
            await update.message.reply_text("Usage: `/entry_mode confirmed|aggressive|extreme [confirm]`")
            return
        if mode == "extreme":
            if len(context.args) < 2 or context.args[1].lower() != "confirm":
                await update.message.reply_text("⚠️ Extreme entry still requires all hard validity gates but has a stricter 90 quality score. Use `/entry_mode extreme confirm` to enable it.")
                return
            self.settings.allow_extreme_entry = True
        self.settings.entry_mode = mode
        await db.save_settings(self.settings)
        await update.message.reply_text(f"✅ Entry mode set to **{mode.upper()}**. Risk remains capped at `{self.settings.max_setup_risk_pct}%` per setup.")

    @admin_only
    async def cmd_safety(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Show the active portfolio, daily, and execution safeguards."""
        streak = await db.get_consecutive_losses()
        text = (
            "🛡 **Execution Safety Dashboard**\n\n"
            f"Entry mode: `{self.settings.entry_mode}`\n"
            f"Risk/setup: `{self.settings.risk_per_trade}%` (hard cap `{self.settings.max_setup_risk_pct}%`)\n"
            f"Open-risk ceiling: `{self.settings.max_total_open_risk_pct}%`\n"
            f"Daily loss stop: `-{self.settings.max_daily_loss_pct}%`\n"
            f"Emergency loss stop: `-{self.settings.absolute_daily_stop_pct}%`\n"
            f"Daily profit stop: `+{self.settings.daily_profit_stop_pct}%`\n"
            f"Loss-streak breaker: `{streak}/{self.settings.max_consecutive_losses}`\n"
            f"Max positions: `{self.settings.max_open_positions}`\n"
            f"Max layers/setup: `{self.settings.max_layers}`\n"
            f"Minimum RR: `1:{self.settings.min_rr_ratio}`\n"
            f"Minimum quality: `{self.settings.min_setup_score}` / 100\n"
            f"Signal TTL: `{self.settings.max_signal_age_minutes} min`"
        )
        await update.message.reply_text(text, parse_mode="Markdown")

    @admin_only
    async def cmd_baskets(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Show active setup baskets, reserved risk, and layer progress."""
        baskets = await db.get_open_baskets()
        if not baskets:
            await update.message.reply_text("No active bot-managed baskets. Manual MT5 positions are shown in `/positions`.")
            return
        lines = ["📦 **Active Trade Baskets**\n"]
        for basket in baskets:
            layers = await db.get_basket_layers(basket["id"])
            open_layers = sum(1 for layer in layers if layer["status"] == "open")
            planned_layers = sum(1 for layer in layers if layer["status"] == "planned")
            lines.append(
                f"**#{basket['id']} {basket['direction']} {basket['symbol']}**\n"
                f"State: `{basket['state']}` | Risk reserved: `${basket['reserved_risk']:.2f}` / `${basket['max_risk']:.2f}`\n"
                f"Layers: `{open_layers}` open, `{planned_layers}` planned | Initial SL: `{basket['initial_stop']:.5f}`\n"
            )
        await update.message.reply_text("\n".join(lines), parse_mode="Markdown")

    @admin_only
    async def cmd_add_broker(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Add a new MT5 broker configuration."""
        if len(context.args) < 5:
            await update.message.reply_text(
                "Usage: `/add_broker [name] [login] [password] [server] [terminal_path]`\n\n"
                "Example:\n`/add_broker ICMarkets 123456 pass IC-Server C:\\MT5\\terminal64.exe`"
            )
            return

        try:
            from config import BrokerConfig
            new_broker = BrokerConfig(
                name=context.args[0],
                login=int(context.args[1]),
                password=context.args[2],
                server=context.args[3],
                terminal_path=context.args[4]
            )
            self.settings.brokers.append(new_broker)
            await db.save_settings(self.settings)
            await update.message.reply_text(f"✅ **Broker Added**: {new_broker.name}\nRestart the bot to initialize the connection.")
        except Exception as e:
            await update.message.reply_text(f"❌ Error adding broker: {e}")

    @admin_only
    async def cmd_optimize(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Manually trigger self-optimization."""
        await update.message.reply_text("🧠 **Initiating Self-Optimization AI...**\nAnalyzing recent trade history...")
        if self.scheduler:
            await self.scheduler.run_self_optimization()
            await update.message.reply_text("✅ Optimization complete. Scoring weights have been tuned.")
        else:
            await update.message.reply_text("❌ Scheduler not initialized.")

    @admin_only
    async def cmd_journal(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Show the daily AI journal."""
        if self.scheduler and self.scheduler.optimizer:
            journal = await self.scheduler.optimizer.generate_daily_journal()
            await update.message.reply_text(journal, parse_mode="Markdown")
        else:
            await update.message.reply_text("❌ Optimizer not initialized.")

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
                threshold = max(50.0, min(val, 100.0))
                self.settings.score_threshold = threshold
                self.settings.min_setup_score = threshold
                await db.save_settings(self.settings)
                await update.message.reply_text(f"✅ Minimum setup-quality threshold set to {threshold}%.")
            except ValueError:
                await update.message.reply_text("Usage: /score 60")
        else:
            await update.message.reply_text(f"Current minimum setup quality: {self.settings.min_setup_score}%\nUsage: /score 75")

    @admin_only
    async def cmd_daily_limit(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Set daily PnL limit percentage."""
        if context.args:
            try:
                val = float(context.args[0])
                self.settings.daily_profit_stop_pct = max(1.0, min(val, 100.0))
                await db.save_settings(self.settings)
                await update.message.reply_text(f"✅ Daily profit stop set to +{self.settings.daily_profit_stop_pct}%. Daily loss protection remains -{self.settings.max_daily_loss_pct}%.")
            except ValueError:
                await update.message.reply_text("Usage: /daily_limit 20")
        else:
            await update.message.reply_text(f"Current daily profit stop: +{self.settings.daily_profit_stop_pct}%\nDaily loss stop: -{self.settings.max_daily_loss_pct}%\nUsage: /daily_limit 10")

    @admin_only
    async def cmd_cooldown(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Set symbol cooldown minutes."""
        if context.args:
            try:
                val = int(context.args[0])
                self.settings.symbol_cooldown_minutes = max(0, min(val, 1440))
                await db.save_settings(self.settings)
                await update.message.reply_text(f"✅ Symbol cooldown set to {self.settings.symbol_cooldown_minutes} minutes")
            except ValueError:
                await update.message.reply_text("Usage: /cooldown 30")
        else:
            await update.message.reply_text(f"Current cooldown: {self.settings.symbol_cooldown_minutes} min\nUsage: /cooldown 30")

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
        # Always answer to stop the loading spinner
        await query.answer()
        data = query.data

        if data == "main":
            await self.cmd_start(update, context)
        elif data == "cancel":
            await query.edit_message_text("Action cancelled.", reply_markup=keyboards.main_menu())
        elif data == "scan":
            await self.cmd_scan(update, context)
        elif data == "positions":
            await self.cmd_positions(update, context)
        elif data == "settings":
            await self.cmd_settings(update, context)
        elif data == "account":
            await self.cmd_account(update, context)
        elif data == "debug_mt5":
            await self.cmd_debug_mt5(update, context)
        elif data == "history":
            await self.cmd_history(update, context)
        elif data == "safety":
            streak = await db.get_consecutive_losses()
            await query.edit_message_text(
                "🛡 **Execution Safety Dashboard**\n\n"
                f"Entry: `{self.settings.entry_mode}` | Risk/setup: `{self.settings.risk_per_trade}%` (cap `{self.settings.max_setup_risk_pct}%`)\n"
                f"Open-risk ceiling: `{self.settings.max_total_open_risk_pct}%`\n"
                f"Daily stop: `-{self.settings.max_daily_loss_pct}%` / `+{self.settings.daily_profit_stop_pct}%`\n"
                f"Loss streak: `{streak}/{self.settings.max_consecutive_losses}`\n"
                f"Positions/layers: `{self.settings.max_open_positions}/{self.settings.max_layers}`\n"
                f"Min RR / quality: `1:{self.settings.min_rr_ratio}` / `{self.settings.min_setup_score}`\n"
                f"Signal TTL: `{self.settings.max_signal_age_minutes} min`",
                reply_markup=keyboards.main_menu(),
                parse_mode="Markdown",
            )
        elif data == "baskets":
            baskets = await db.get_open_baskets()
            if not baskets:
                text = "📦 **Active Trade Baskets**\n\nNo active bot-managed baskets."
            else:
                lines = ["📦 **Active Trade Baskets**\n"]
                for basket in baskets:
                    layers = await db.get_basket_layers(basket["id"])
                    open_layers = sum(1 for layer in layers if layer["status"] == "open")
                    planned_layers = sum(1 for layer in layers if layer["status"] == "planned")
                    lines.append(f"**#{basket['id']} {basket['direction']} {basket['symbol']}**\nState: `{basket['state']}` | Risk: `${basket['reserved_risk']:.2f}/${basket['max_risk']:.2f}`\nLayers: `{open_layers}` open, `{planned_layers}` planned\n")
                text = "\n".join(lines)
            await query.edit_message_text(text, reply_markup=keyboards.main_menu(), parse_mode="Markdown")
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
            await query.edit_message_text(f"✅ Closed {closed} positions.", reply_markup=keyboards.main_menu())
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
        elif data == "toggle_aggressive":
            self.settings.aggressive_mode = not self.settings.aggressive_mode
            await db.save_settings(self.settings)
            status = "ON 🔥" if self.settings.aggressive_mode else "OFF 🛡"
            await query.edit_message_text(f"Aggressive Growth is now **{status}**.", reply_markup=keyboards.settings_menu())
        elif data == "toggle_scalping":
            is_scalping = "M1" in self.settings.timeframes
            if is_scalping:
                self.settings.timeframes = [tf for tf in self.settings.timeframes if tf not in ["M1", "M5"]]
                if not self.settings.timeframes: self.settings.timeframes = ["M15", "H1", "H4"]
                status = "OFF 🛡"
            else:
                new_tfs = ["M1", "M5"]
                for tf in self.settings.timeframes:
                    if tf not in new_tfs: new_tfs.append(tf)
                self.settings.timeframes = new_tfs
                status = "ON ⏱"
            await db.save_settings(self.settings)
            await query.edit_message_text(f"Scalping Mode is now **{status}**.", reply_markup=keyboards.settings_menu())
        elif data == "toggle_index_focus":
            self.settings.index_focus = not self.settings.index_focus
            if self.settings.index_focus:
                indices = [s for s in self.settings.enabled_symbols if "Volatility" in s]
                others = [s for s in self.settings.enabled_symbols if "Volatility" not in s]
                self.settings.enabled_symbols = indices + others
            await db.save_settings(self.settings)
            status = "ON 🎯" if self.settings.index_focus else "OFF 🛡"
            await query.edit_message_text(f"Index Focus is now **{status}**.", reply_markup=keyboards.settings_menu())
            
        elif data == "toggle_sentiment":
            self.settings.sentiment_analysis_enabled = not self.settings.sentiment_analysis_enabled
            await db.save_settings(self.settings)
            status = "ON 🌊" if self.settings.sentiment_analysis_enabled else "OFF 🛡"
            await query.edit_message_text(f"AI Sentiment is now **{status}**.", reply_markup=keyboards.settings_menu())
            
        elif data == "toggle_optimization":
            self.settings.self_optimization_enabled = not self.settings.self_optimization_enabled
            await db.save_settings(self.settings)
            status = "ON 🧠" if self.settings.self_optimization_enabled else "OFF 🛡"
            await query.edit_message_text(f"Self-Optimization is now **{status}**.", reply_markup=keyboards.settings_menu())
            
        elif data == "toggle_arbitrage":
            self.settings.arbitrage_enabled = not self.settings.arbitrage_enabled
            await db.save_settings(self.settings)
            status = "ON ⚡️" if self.settings.arbitrage_enabled else "OFF 🛡"
            await query.edit_message_text(f"Arbitrage Monitor is now **{status}**.", reply_markup=keyboards.settings_menu())
            
        elif data == "multi_broker_menu":
            text = "🏦 **Multi-Broker Management**\n\nActive Brokers:\n"
            if not self.settings.brokers:
                text += "_No additional brokers configured._"
            else:
                for b in self.settings.brokers:
                    text += f"• {b.name} ({b.server}) - {'✅' if b.is_active else '❌'}\n"
            
            text += "\nUse `/add_broker` to add a new MT5 terminal."
            await query.edit_message_text(text, reply_markup=keyboards.confirm_keyboard("main"), parse_mode="Markdown")
        elif data == "confirm_expert_mode":
            await query.edit_message_text(
                "⚠️ Activate Expert Mode? This will focus on high-probability institutional pairs.",
                reply_markup=keyboards.confirm_keyboard("expert_mode")
            )
        elif data == "confirm_confirm_expert_mode":
            expert_selection = ["Volatility 75 Index", "Volatility 100 Index", "Volatility 10 Index", "Volatility 25 Index", "EURUSD", "GBPUSD", "USDJPY", "XAUUSD"]
            for s in expert_selection:
                if s not in self.settings.symbols: self.settings.symbols.append(s)
            self.settings.enabled_symbols = [s for s in expert_selection]
            await db.save_settings(self.settings)
            await query.edit_message_text("🏆 **EXPERT MODE ACTIVATED**", reply_markup=keyboards.settings_menu())
        elif data == "set_target":
            await query.edit_message_text("Use command: `/target [amount]` to set a balance goal.", reply_markup=keyboards.settings_menu())
        elif data == "set_virtual_balance":
            await query.edit_message_text("Use command: `/set_balance [amount]` to set a virtual balance for risk.", reply_markup=keyboards.settings_menu())
        elif data == "set_risk":
            await query.edit_message_text(
                f"Current risk/setup: {self.settings.risk_per_trade}%\nUse command: /risk 0.75\n(Hard range: 0.1% - {min(self.settings.max_setup_risk_pct, 1.0)}%)",
                reply_markup=keyboards.settings_menu()
            )
        elif data == "set_entry_mode":
            await query.edit_message_text(
                f"Current entry mode: **{self.settings.entry_mode.upper()}**\n\n"
                "Use `/entry_mode confirmed` for LTF confirmation.\n"
                "Use `/entry_mode aggressive` for earlier structure-confirmed entries.\n"
                "Use `/entry_mode extreme confirm` only for 90+ quality setups.",
                reply_markup=keyboards.settings_menu(),
                parse_mode="Markdown",
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
            await query.edit_message_text(
                f"Current cooldown: {self.settings.symbol_cooldown_minutes} min.\nUse command: `/cooldown 15` to change.",
                reply_markup=keyboards.settings_menu()
            )
        elif data == "set_daily_limit":
            await query.edit_message_text(
                f"Daily profit stop: +{self.settings.daily_profit_stop_pct}%\n"
                f"Daily loss stop: -{self.settings.max_daily_loss_pct}%\n"
                f"Use command: `/daily_limit 10` to set the profit stop.",
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
                "Toggle symbols (✅ = enabled):",
                reply_markup=keyboards.symbol_select_keyboard(all_symbols, self.settings.enabled_symbols)
            )
        elif data.startswith("sym_"):
            sym = data.replace("sym_", "")
            if sym in self.settings.enabled_symbols:
                self.settings.enabled_symbols.remove(sym)
            else:
                self.settings.enabled_symbols.append(sym)
            await db.save_settings(self.settings)
            all_symbols = [
                "EURUSD", "GBPUSD", "USDJPY", "AUDUSD", "USDCAD", "XAUUSD",
                "Volatility 75 Index", "Volatility 100 Index",
                "Boom 500 Index", "Boom 1000 Index",
                "Crash 500 Index", "Crash 1000 Index",
            ]
            await query.edit_message_text(
                "Toggle symbols (✅ = enabled):",
                reply_markup=keyboards.symbol_select_keyboard(all_symbols, self.settings.enabled_symbols)
            )
        elif data == "set_timeframes":
            all_tfs = ["M1", "M5", "M15", "M30", "H1", "H4", "D1"]
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
            all_tfs = ["M1", "M5", "M15", "M30", "H1", "H4", "D1"]
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
        elif data.startswith("manage_"):
            ticket = int(data.split("_")[1])
            await query.answer(f"Analyzing Position #{ticket}...")
            if self.scheduler:
                msg = await self.scheduler.manual_manage_position(ticket)
                await query.message.reply_text(msg, parse_mode="Markdown")
            else:
                await query.message.reply_text("❌ Scheduler not initialized.")
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
        app.add_handler(CommandHandler("aggressive", self.cmd_aggressive))
        app.add_handler(CommandHandler("target", self.cmd_target))
        app.add_handler(CommandHandler("scalping", self.cmd_scalping))
        app.add_handler(CommandHandler("toggle_symbol", self.cmd_toggle_symbol))
        app.add_handler(CommandHandler("expert_mode", self.cmd_expert_mode))
        app.add_handler(CommandHandler("focus_indices", self.cmd_focus_indices))
        app.add_handler(CommandHandler("profile", self.cmd_profile))
        app.add_handler(CommandHandler("mode", self.cmd_mode))
        app.add_handler(CommandHandler("risk", self.cmd_risk))
        app.add_handler(CommandHandler("entry_mode", self.cmd_entry_mode))
        app.add_handler(CommandHandler("safety", self.cmd_safety))
        app.add_handler(CommandHandler("baskets", self.cmd_baskets))
        app.add_handler(CommandHandler("rr", self.cmd_rr))
        app.add_handler(CommandHandler("add_broker", self.cmd_add_broker))
        app.add_handler(CommandHandler("optimize", self.cmd_optimize))
        app.add_handler(CommandHandler("journal", self.cmd_journal))
        app.add_handler(CommandHandler("manage", self.cmd_manage))
        app.add_handler(CommandHandler("score", self.cmd_score))
        app.add_handler(CommandHandler("daily_limit", self.cmd_daily_limit))
        app.add_handler(CommandHandler("cooldown", self.cmd_cooldown))
        app.add_handler(CommandHandler("backtest", self.cmd_backtest))
        app.add_handler(CommandHandler("sessions", self.cmd_sessions))
        app.add_handler(CommandHandler("sessions_all", self.cmd_sessions_all))
        app.add_handler(CommandHandler("news", self.cmd_news))
        app.add_handler(CallbackQueryHandler(self.handle_callback))
        self.app = app
