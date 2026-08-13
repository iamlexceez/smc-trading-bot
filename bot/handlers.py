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
from datetime import datetime, timezone
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
from bot.account_views import LiveAccountViews
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

    def account_views(self) -> LiveAccountViews:
        """Create a read-only view service using the active broker executor."""
        return LiveAccountViews(self.executor, self.settings.trading_mode)

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
            
            # Connect and rebuild the market universe from the newly selected
            # account before any scan can continue.
            connected = await self.executor.connect()
            if connected and self.scheduler:
                await self.scheduler.data_provider.init()
                await self.scheduler.refresh_market_universe()
            return connected
        except Exception as e:
            logger.error(f"Reconnection error: {e}")
            return False

    # ─── Commands ──────────────────────────────────────────

    async def _render_menu(self, update: Update, text: str, markup=None) -> None:
        """Render one Markdown monitoring view for a command or inline callback."""
        markup = markup or keyboards.main_menu()
        if update.callback_query:
            await update.callback_query.edit_message_text(text, reply_markup=markup, parse_mode="Markdown")
        elif update.message:
            await update.message.reply_text(text, reply_markup=markup, parse_mode="Markdown")

    async def _render_plain_menu(self, update: Update, text: str, markup=None) -> None:
        """Render untrusted broker metadata without Markdown entity parsing."""
        markup = markup or keyboards.main_menu()
        safe_text = str(text)[:3900]
        if update.callback_query:
            await update.callback_query.edit_message_text(safe_text, reply_markup=markup)
        elif update.message:
            await update.message.reply_text(safe_text, reply_markup=markup)

    async def _dashboard_text(self) -> str:
        performance = await db.get_performance_summary(self.settings.trading_mode, days=1)
        model = await db.get_active_model(self.settings.trading_mode)
        universe = self.settings.symbol_status or {}
        active_count = len(self.settings.enabled_symbols)
        available_count = len(self.settings.available_symbols)
        model_text = model["version"] if model else "baseline pending"
        forward = await db.get_active_forward_experiment(self.settings.trading_mode)
        experiment_text = forward.get("model_version", "none") if forward else "none"
        return "\n".join([
            "🤖 **DERIV AUTONOMOUS RESEARCH SYSTEM**",
            f"Mode: `{self.settings.trading_mode.upper()}` | Autonomous execution: `{'ON' if self.settings.auto_trade and not self.settings.is_paused else 'OFF'}`",
            f"Broker universe: `{active_count}` active / `{available_count}` available Deriv Synthetic Indices or Gold",
            f"Today: `{performance['trades']}` closed trades | P/L `${performance['pnl']:.2f}` | win rate `{performance['win_rate']:.1f}%`",
            f"Champion: `{model_text}` | Forward-DEMO challenger: `{experiment_text}`",
            f"Research engine: `{'enabled' if self.settings.self_optimization_enabled else 'disabled'}` — policy variables are learned from evidence, not fixed global caps.",
            "\nUse the research controls below. LIVE always requires a separate explicit confirmation.",
        ])

    @admin_only
    async def cmd_start(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Show the monitoring dashboard."""
        await self.reload_settings()
        await self._render_menu(update, await self._dashboard_text())

    @admin_only
    async def cmd_dashboard(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Alias for the primary dashboard."""
        await self.cmd_start(update, context)

    @admin_only
    async def cmd_help(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Show the deliberately small operational command surface."""
        await self._render_menu(
            update,
            "**Research commands**\n\n"
            "`/dashboard` — current autonomous-system status\n"
            "`/markets` — broker-verified Deriv universe\n"
            "`/positions` — active broker positions and recorded policy actions\n"
            "`/learning` — measured observations and next objective\n"
            "`/experiments` — immutable policy experiment lifecycle\n"
            "`/champion` — current validated policy and its evidence\n"
            "`/challengers` — independent candidates in forward DEMO\n"
            "`/research` — falsifiable hypotheses and candidate values\n"
            "`/performance` — DEMO/LIVE-isolated statistics\n"
            "`/backtest <symbol> <tf> <days>` — causal policy backtest\n"
            "`/activity [detailed|essential|off]` — chart-study notification mode\n"
            "`/settings` — autonomy, alerts, and explicit DEMO/LIVE controls\n"
            "`/emergency` — pause new execution and optionally close positions\n\n"
            "Trading-policy controls are intentionally not manual commands. DEMO research evaluates risk, RR, feature combinations, layering, and management through versioned experiments. Broker validity, synchronization, and emergency controls remain mandatory.",
        )

    @admin_only
    async def cmd_activity(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Show or change the closed-candle chart-activity alert mode."""
        if context.args:
            requested = context.args[0].lower()
            if requested not in {"detailed", "essential", "off"}:
                await update.message.reply_text("Usage: `/activity detailed`, `/activity essential`, or `/activity off`", parse_mode="Markdown")
                return
            self.settings.chart_activity_level = requested
            self.settings.chart_activity_notifications = requested != "off"
            await db.save_settings(self.settings)

        mode = self.settings.chart_activity_level
        state = "ON" if self.settings.chart_activity_notifications and mode != "off" else "OFF"
        text = "\n".join([
            "📡 **CHART ACTIVITY ALERTS**",
            f"Status: `{state}` | Mode: `{mode.upper()}`",
            f"Rejected setup alerts: `{'ON' if self.settings.chart_activity_include_rejections else 'OFF'}`",
            f"Duplicate cooldown: `{self.settings.chart_activity_cooldown_seconds}s` per symbol and stage",
            "",
            "**Detailed** reports closed-candle study, structure, hard-gate rejections, validated setups, final risk review, broker submission, execution, and management actions.",
            "**Essential** reports execution-critical broker, safety, and management events only.",
            "Use `/activity detailed`, `/activity essential`, or `/activity off`.",
        ])
        await self._render_menu(
            update,
            text,
            keyboards.activity_menu(mode, self.settings.chart_activity_include_rejections),
        )

    @admin_only
    async def cmd_markets(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Show broker-backed accepted/rejected universe decisions and audit location."""
        if not self.scheduler:
            await self._render_menu(update, "💹 **BROKER UNIVERSE**\n\nScheduler is unavailable; no broker discovery audit can be produced.")
            return
        await self.scheduler.refresh_market_universe()
        universe = self.scheduler.market_universe
        accepted = universe.accepted_records
        rejected = universe.rejected_records
        audit_paths = self.scheduler.last_universe_audit_paths
        accepted_preview = accepted[:20]
        rejected_preview = rejected[:10]
        lines = [
            "MT5 BROKER-VERIFIED UNIVERSE",
            f"Mode: {self.settings.trading_mode.upper()} | Returned: {len(universe.records)} | ACCEPTED: {len(accepted)} | REJECTED: {len(rejected)}",
            "",
            "ACCEPTED — executable Deriv Synthetic Indices / Gold",
            *( [f"- {record.symbol} — {record.category}; {record.trade_mode_name}; volume {record.volume_min}–{record.volume_max} step {record.volume_step}" for record in accepted_preview] or ["None. Execution remains fail-closed."] ),
            "",
            "REJECTED — first 10 decisions",
            *( [f"- {record.symbol} — {record.decision_reason}" for record in rejected_preview] or ["None."] ),
        ]
        if len(accepted) > len(accepted_preview):
            lines.append(f"… plus {len(accepted) - len(accepted_preview)} more accepted records in the full audit.")
        if len(rejected) > len(rejected_preview):
            lines.append(f"… plus {len(rejected) - len(rejected_preview)} more rejected records in the full audit.")
        if universe.last_refresh_error:
            lines.extend(["", f"MT5 retrieval warning: {universe.last_refresh_error}"])
        if audit_paths:
            lines.extend(["", f"Complete metadata audit: {audit_paths[1]}", f"Machine-readable audit: {audit_paths[0]}"])
        lines.extend(["", "No non-broker or guessed instrument is enabled. Forex, crypto, and all unsupported products remain rejected."])
        await self._render_plain_menu(update, "\n".join(lines))

    @admin_only
    async def cmd_performance(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Show fresh broker-source daily performance, including current floating P/L."""
        await self._render_plain_menu(update, await self.account_views().daily_performance())

    @admin_only
    async def cmd_learning(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Show factual learning status rather than a generic confidence claim."""
        mode = self.settings.trading_mode
        model = await db.get_active_model(mode)
        recent = await db.get_recent_optimization_runs(mode, limit=1)
        performance = await db.get_performance_summary(mode, days=30)
        profile_lines = []
        for symbol, profile in list(profiler.profiles.items())[:5]:
            profile_lines.append(f"• `{symbol}` — completed outcomes `{profile.sample_size}`, expectancy `{profile.expectancy_r:.2f}R`")
        model_text = model["version"] if model else "baseline pending"
        decision = recent[0]["decision"] if recent else "no optimization run yet"
        text = [
            "🧠 **LEARNING STATUS**",
            f"Mode: `{mode.upper()}` | Active model: `{model_text}` | Latest governance decision: `{decision}`",
            f"Completed 30-day outcomes: `{performance['trades']}` | P/L `${performance['pnl']:.2f}` | win rate `{performance['win_rate']:.1f}%`",
            "",
            "**Current symbol evidence**",
            *(profile_lines or ["No in-memory profile yet. The next broker-candle scan will build observable profiles; completed outcomes are required before outcome statistics affect settings."]),
            "",
            "**Next objective**",
            f"Collect at least `{self.settings.optimization_min_sample_size}` completed DEMO R-recorded outcomes, then compare independently specified policies through train, validation, out-of-sample, and forward-DEMO evidence. Broker and software integrity remain mandatory; risk, RR, features, layering, and management are experimental.",
        ]
        await self._render_menu(update, "\n".join(text))

    @admin_only
    async def cmd_model(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Expose champion version, soft settings, and latest model decision."""
        mode = self.settings.trading_mode
        model = await db.get_active_model(mode)
        runs = await db.get_recent_optimization_runs(mode, limit=1)
        if not model:
            text = "🔄 **MODEL GOVERNANCE**\n\nNo champion exists yet. The next DEMO optimization cycle will create a baseline version without changing any safety limits."
        else:
            params = model.get("parameters", {})
            performance = model.get("performance", {})
            oos = performance.get("out_of_sample", {})
            latest = runs[0]["decision"] if runs else "none"
            text = "\n".join([
                "🔄 **MODEL GOVERNANCE**",
                f"Champion: `{model['version']}` | Status: `{model['status']}` | Latest decision: `{latest}`",
                f"Soft settings: quality floor `{params.get('min_setup_score', 0):.1f}`, preferred risk `{params.get('preferred_risk_pct', 0):.2f}%`, preferred trade cap `{params.get('preferred_max_trades_per_day', 0)}`",
                f"Stored OOS evidence: `{oos.get('sample_size', 0)}` trades | expectancy `{oos.get('expectancy_r', 0):.2f}R` | maximum drawdown `{oos.get('max_drawdown_r', 0):.2f}R`",
                "A challenger may be promoted only in DEMO after positive unseen evidence and acceptable drawdown. LIVE self-promotion is blocked.",
            ])
        await self._render_menu(update, text)

    @admin_only
    async def cmd_experiments(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Show immutable experiment lifecycle records for the current account mode."""
        experiments = await db.list_policy_experiments(self.settings.trading_mode, limit=8)
        if not experiments:
            text = "🧪 **EXPERIMENTS**\n\nNo policy experiment has been created yet. The research engine will first collect completed DEMO outcomes, generate hypotheses, then begin chronological comparisons."
        else:
            lines = ["🧪 **EXPERIMENTS**", ""]
            for item in experiments:
                policy = item.get("policy", {})
                evaluation = item.get("evaluation", {})
                oos = evaluation.get("out_of_sample", {})
                lines.append(
                    f"• `#{item['id']}` `{item['status'].upper()}` — `{item.get('model_version') or 'unversioned'}`\n"
                    f"  Entry `{policy.get('entry_model', 'n/a')}` | Risk `{policy.get('risk_pct', 'n/a')}%` | RR `{policy.get('rr_target') or 'market-derived'}` | Layers `{policy.get('max_layers', 0)}`\n"
                    f"  OOS sample `{oos.get('sample_size', 0)}` | expectancy `{oos.get('expectancy_r', 0):.2f}R`\n"
                )
            text = "\n".join(lines)
        await self._render_menu(update, text)

    @admin_only
    async def cmd_champion(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Show the current validated champion as a transparent policy object."""
        model = await db.get_active_model(self.settings.trading_mode)
        if not model:
            text = "🏆 **CHAMPION**\n\nNo champion has been seeded yet. DEMO research will create a non-privileged baseline first."
        else:
            policy = model.get("parameters", {})
            evidence = model.get("performance", {})
            forward = evidence.get("forward_demo", {})
            oos = evidence.get("out_of_sample", {})
            metric = forward or oos
            text = "\n".join([
                "🏆 **CURRENT CHAMPION POLICY**",
                f"Version: `{model['version']}` | Created: `{model.get('created_at', 'n/a')}`",
                f"Entry: `{policy.get('entry_model', 'hybrid')}` | Required features: `{', '.join(policy.get('required_features', [])) or 'none'}`",
                f"Risk: `{policy.get('risk_model', 'n/a')}` at `{policy.get('risk_pct', 'n/a')}%` | RR: `{policy.get('rr_target') or 'market-derived'}`",
                f"SL: `{policy.get('stop_model', 'n/a')}` | TP: `{policy.get('target_model', 'n/a')}` | Layers: `{policy.get('max_layers', 0)}` / `{policy.get('layer_style', 'none')}`",
                f"Management: BE `{policy.get('breakeven_model', 'none')}`, trailing `{policy.get('trailing_model', 'none')}`, partial `{policy.get('partial_exit_model', 'none')}`",
                f"Evidence: sample `{metric.get('sample_size', 0)}` | expectancy `{metric.get('expectancy_r', 0):.2f}R` | drawdown `{metric.get('max_drawdown_r', 0):.2f}R`",
                "LIVE is never activated or changed by policy research.",
            ])
        await self._render_menu(update, text)

    @admin_only
    async def cmd_challengers(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Show candidates independently awaiting or undergoing forward-DEMO evidence."""
        experiments = await db.list_policy_experiments(self.settings.trading_mode, limit=20)
        challengers = [item for item in experiments if item.get("status") in {"candidate", "forward_demo", "rejected_historical"}]
        if not challengers:
            text = "⚔️ **CHALLENGERS**\n\nNo active challengers. The engine will generate new falsifiable candidates from completed DEMO evidence."
        else:
            lines = ["⚔️ **CHALLENGERS**", ""]
            for item in challengers:
                policy = item.get("policy", {})
                lines.append(
                    f"`#{item['id']}` `{item['status'].upper()}` — `{item.get('model_version') or 'candidate'}`\n"
                    f"Risk `{policy.get('risk_pct', 'n/a')}%`, RR `{policy.get('rr_target') or 'market-derived'}`, entry `{policy.get('entry_model', 'hybrid')}`\n"
                    f"Reason: {item.get('reason', 'n/a')}\n"
                )
            text = "\n".join(lines)
        await self._render_menu(update, text)

    @admin_only
    async def cmd_research(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Show unresolved falsifiable hypotheses and the next research question."""
        hypotheses = await db.get_open_hypotheses(self.settings.trading_mode)
        if not hypotheses:
            text = "🔬 **RESEARCH QUEUE**\n\nNo stored hypotheses yet. The next completed DEMO outcomes will seed the hypothesis engine."
        else:
            lines = ["🔬 **RESEARCH QUEUE**", ""]
            for item in hypotheses[:8]:
                evidence = item.get("evidence", {})
                lines.append(
                    f"• `{item['hypothesis_key']}` — {item['statement']}\n"
                    f"  Candidates: `{item.get('candidate_values', [])}` | evidence sample `{evidence.get('sample_size', 0)}`\n"
                )
            text = "\n".join(lines)
        await self._render_menu(update, text)

    @admin_only
    async def cmd_backtest_help(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Explain the broker-history-only causal backtest interface."""
        await self._render_menu(update, "🧪 **CAUSAL BACKTEST**\n\nUse `/backtest <broker-symbol> <timeframe> <days>`. The bot requests closed historical candles from the connected Deriv MT5 account and refuses unavailable or unsupported symbols. Backtests use the same validity gates, structural management, and risk ceilings as live execution.")

    @admin_only
    async def cmd_emergency(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Immediately halt new execution, then require confirmation to close positions."""
        self.settings.is_paused = True
        self.settings.auto_trade = False
        await db.save_settings(self.settings)
        text = "🚨 **EMERGENCY STOP ACTIVE**\n\nNew trade execution has been paused and autonomous execution is OFF. Existing positions remain open until you explicitly confirm closing them below."
        if update.callback_query:
            await update.callback_query.edit_message_text(text, reply_markup=keyboards.confirm_keyboard("emergency_close_all"), parse_mode="Markdown")
        else:
            await update.message.reply_text(text, reply_markup=keyboards.confirm_keyboard("emergency_close_all"), parse_mode="Markdown")

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
            await update.message.reply_text("Usage: `/profile [broker-symbol]` (choose a broker-verified Deriv market from `/markets`)")
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
        """Show every current MT5 position with read-only bot provenance."""
        reply_target = update.callback_query.message if update.callback_query else update.message
        for view in await self.account_views().positions():
            await reply_target.reply_text(view, parse_mode=None)
        return

        positions = await self.executor.get_open_positions()
        if not positions:
            await reply_target.reply_text("No open positions.")
            return

        total_profit = 0.0
        for position in positions:
            emoji = "🟢" if position.profit >= 0 else "🔴"
            basket = await db.get_basket_for_ticket(position.ticket, self.settings.trading_mode)
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
    async def cmd_position(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Show one current MT5 position by ticket without changing it."""
        if not context.args:
            await self._render_plain_menu(update, "Usage: /position <MT5 ticket>")
            return
        try:
            ticket = int(context.args[0])
        except ValueError:
            await self._render_plain_menu(update, "Position ticket must be a number.")
            return
        await self._render_plain_menu(update, await self.account_views().position_detail(ticket))

    @admin_only
    async def cmd_orders(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Show live MT5 pending orders without changing them."""
        await self._render_plain_menu(update, await self.account_views().orders())

    @admin_only
    async def cmd_history(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Show MT5 closed-deal history for 1, 7, or 30 days."""
        days = 1
        if context.args:
            try:
                days = int(context.args[0])
            except ValueError:
                await self._render_plain_menu(update, "Usage: /history [1|7|30]")
                return
        if days not in {1, 7, 30}:
            await self._render_plain_menu(update, "History supports only 1, 7, or 30 broker-history days.")
            return
        await self._render_plain_menu(update, await self.account_views().history(days))

    @admin_only
    async def cmd_exposure(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Show read-only live account exposure."""
        await self._render_plain_menu(update, await self.account_views().exposure())

    @admin_only
    async def cmd_health(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Show fresh MT5 connection and reconciliation health."""
        await self._render_plain_menu(update, await self.account_views().health())

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
            "⚙️ **SYSTEM SETTINGS**\n\n"
            "Trading policy is selected by the versioned DEMO research engine; this screen intentionally does not impose risk, RR, score, drawdown, layering, or management limits.\n\n"
            f"Auto-trade: {'ON' if self.settings.auto_trade else 'OFF'}\n"
            f"Emergency pause: {'ON' if self.settings.is_paused else 'OFF'}\n"
            f"Mode: {self.settings.trading_mode.upper()} (LIVE requires explicit confirmation)\n"
            f"Chart activity: {self.settings.chart_activity_level.upper()}\n"
            f"Broker universe: {len(self.settings.enabled_symbols)} enabled Deriv Synthetic Indices / Gold instruments\n"
            f"Timeframes: {', '.join(self.settings.timeframes)}\n"
            f"Research: {'ON' if self.settings.self_optimization_enabled else 'OFF'} | minimum evidence: {self.settings.optimization_min_sample_size} completed DEMO R outcomes"
        )

    @admin_only
    async def cmd_account(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Show fresh broker account overview without modifying MT5 state."""
        await self._render_plain_menu(update, await self.account_views().account_overview())
        return

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
    async def cmd_local_history_legacy(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Legacy internal database history retained for non-command compatibility."""
        trades = await db.get_trade_history(limit=15, account_mode=self.settings.trading_mode)
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
        expert_selection = list(self.settings.available_symbols)
        
        # Add any expert symbols that aren't in the available list yet
        for s in expert_selection:
            if s not in self.settings.symbols:
                self.settings.symbols.append(s)
        
        # Set enabled symbols to exactly the expert selection
        self.settings.enabled_symbols = [s for s in expert_selection]
        
        await db.save_settings(self.settings)
        await update.message.reply_text(
            "🏆 **EXPERT MODE ACTIVATED**\n\n"
            "The bot is now restricted to the currently broker-verified Deriv Synthetic Indices and Gold universe.\n\n"
            "Unavailable or unsupported broker instruments remain disabled."
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
                    self.settings.live_trading_confirmed_at = None
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
        streak = await db.get_consecutive_losses(account_mode=self.settings.trading_mode)
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
        baskets = await db.get_open_baskets(self.settings.trading_mode)
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
    async def cmd_loss_limit(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Set the normal daily loss stop while retaining a separate emergency stop."""
        if not context.args:
            await update.message.reply_text(
                f"Daily loss stop: -{self.settings.max_daily_loss_pct}% | Emergency stop: -{self.settings.absolute_daily_stop_pct}%\n"
                "Usage: `/loss_limit 3`"
            )
            return
        try:
            value = float(context.args[0])
            value = max(0.5, min(value, 10.0))
            self.settings.max_daily_loss_pct = value
            self.settings.absolute_daily_stop_pct = max(value, min(10.0, value + 1.0))
            await db.save_settings(self.settings)
            await update.message.reply_text(f"✅ Daily loss stop set to **-{value}%**; emergency stop set to **-{self.settings.absolute_daily_stop_pct}%**.")
        except ValueError:
            await update.message.reply_text("Usage: `/loss_limit 3`")

    @admin_only
    async def cmd_open_risk(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Set the account-wide protected loss ceiling for all open positions."""
        if not context.args:
            await update.message.reply_text(f"Current total open-risk ceiling: {self.settings.max_total_open_risk_pct}%\nUsage: `/open_risk 3`")
            return
        try:
            value = max(0.5, min(float(context.args[0]), 5.0))
            self.settings.max_total_open_risk_pct = value
            await db.save_settings(self.settings)
            await update.message.reply_text(f"✅ Total open-risk ceiling set to **{value}%**.")
        except ValueError:
            await update.message.reply_text("Usage: `/open_risk 3`")

    @admin_only
    async def cmd_layers(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Set the maximum number of planned confirmation-only layers per setup."""
        if not context.args:
            await update.message.reply_text(f"Current maximum layers/setup: {self.settings.max_layers}\nUsage: `/layers 3`")
            return
        try:
            value = max(1, min(int(context.args[0]), 4))
            self.settings.max_layers = value
            await db.save_settings(self.settings)
            await update.message.reply_text(f"✅ Maximum planned layers per setup set to **{value}**. Existing baskets are unchanged.")
        except ValueError:
            await update.message.reply_text("Usage: `/layers 3`")

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
            await update.message.reply_text("Usage: /backtest [broker-listed Deriv symbol] [timeframe] [days]")
            return

        symbol = context.args[0]
        timeframe = context.args[1] if len(context.args) > 1 else "H1"
        try:
            days = int(context.args[2]) if len(context.args) > 2 else 180
        except ValueError:
            days = 180

        await self.reload_settings()
        if symbol not in self.settings.available_symbols:
            await update.message.reply_text("❌ That symbol is not currently a broker-verified Deriv Synthetic Index or Gold instrument.")
            return

        await update.message.reply_text(
            f"📊 Running broker-history backtest: {symbol} {timeframe} over {days} days...\n"
            f"This may take a moment."
        )

        try:
            from backtest.runner import run_backtest
            result = await run_backtest(symbol, timeframe, days, self.settings, executor=self.executor)
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

        if data in {"main", "dashboard"}:
            await self.cmd_dashboard(update, context)
        elif data == "account":
            await self.cmd_account(update, context)
        elif data == "positions":
            await self.cmd_positions(update, context)
        elif data == "orders":
            await self.cmd_orders(update, context)
        elif data == "exposure":
            await self.cmd_exposure(update, context)
        elif data == "health":
            await self.cmd_health(update, context)
        elif data == "markets":
            await self.cmd_markets(update, context)
        elif data == "learning":
            await self.cmd_learning(update, context)
        elif data == "performance":
            await self.cmd_performance(update, context)
        elif data in {"model", "champion"}:
            await self.cmd_champion(update, context)
        elif data == "experiments":
            await self.cmd_experiments(update, context)
        elif data == "challengers":
            await self.cmd_challengers(update, context)
        elif data == "research":
            await self.cmd_research(update, context)
        elif data == "backtest_help":
            await self.cmd_backtest_help(update, context)
        elif data == "emergency":
            await self.cmd_emergency(update, context)
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
            streak = await db.get_consecutive_losses(account_mode=self.settings.trading_mode)
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
            baskets = await db.get_open_baskets(self.settings.trading_mode)
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
        elif data in {"confirm_close_all", "confirm_emergency_close_all"}:
            closed = await self.executor.close_all_positions()
            await query.edit_message_text(
                f"🚨 Emergency state remains active. Closed `{closed}` broker position(s).",
                reply_markup=keyboards.main_menu(),
                parse_mode="Markdown",
            )
        elif data == "activity_menu":
            mode = self.settings.chart_activity_level
            status = "ON" if self.settings.chart_activity_notifications and mode != "off" else "OFF"
            await query.edit_message_text(
                "📡 **CHART ACTIVITY ALERTS**\n\n"
                f"Status: `{status}` | Mode: `{mode.upper()}`\n"
                f"Rejected setup alerts: `{'ON' if self.settings.chart_activity_include_rejections else 'OFF'}`\n"
                f"Duplicate cooldown: `{self.settings.chart_activity_cooldown_seconds}s` per symbol and stage\n\n"
                "Detailed follows closed-candle study and every decision stage. Essential reports only execution-critical broker, safety, and management events.",
                reply_markup=keyboards.activity_menu(mode, self.settings.chart_activity_include_rejections),
                parse_mode="Markdown",
            )
        elif data in {"activity_detailed", "activity_essential", "activity_off"}:
            mode = data.removeprefix("activity_")
            self.settings.chart_activity_level = mode
            self.settings.chart_activity_notifications = mode != "off"
            await db.save_settings(self.settings)
            await query.edit_message_text(
                f"📡 Chart activity mode set to **{mode.upper()}**.",
                reply_markup=keyboards.activity_menu(mode, self.settings.chart_activity_include_rejections),
                parse_mode="Markdown",
            )
        elif data == "activity_rejections":
            self.settings.chart_activity_include_rejections = not self.settings.chart_activity_include_rejections
            await db.save_settings(self.settings)
            mode = self.settings.chart_activity_level
            await query.edit_message_text(
                f"📡 Rejected-setup alerts are now **{'ON' if self.settings.chart_activity_include_rejections else 'OFF'}**.",
                reply_markup=keyboards.activity_menu(mode, self.settings.chart_activity_include_rejections),
                parse_mode="Markdown",
            )
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
            if self.scheduler and not self.settings.is_paused:
                asyncio.create_task(self.scheduler.activate_and_scan_now())
                activation_text = "Auto-Trade is now ON. Broker universe refresh and the first safe scan have started immediately."
            elif self.settings.is_paused:
                activation_text = "Auto-Trade is ON, but the emergency pause remains active. Resume/clear the pause before scanning can begin."
            else:
                activation_text = "Auto-Trade is now ON, but the scheduler is unavailable for an immediate scan."
            await query.edit_message_text(
                activation_text,
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
                self.settings.live_trading_confirmed_at = None
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
                self.settings.live_trading_confirmed_at = datetime.now(timezone.utc).isoformat()
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
            expert_selection = list(self.settings.available_symbols)
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
                f"Use `/daily_limit 10` for the profit stop or `/loss_limit 3` for the loss stop.",
                reply_markup=keyboards.settings_menu()
            )
        elif data == "set_symbols":
            all_symbols = list(self.settings.available_symbols)
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
            all_symbols = list(self.settings.available_symbols)
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
        # The command surface intentionally mirrors the autonomous-system
        # dashboard. Legacy manual strategy and arbitrary market commands are
        # no longer registered.
        app.add_handler(CommandHandler("start", self.cmd_start))
        app.add_handler(CommandHandler("dashboard", self.cmd_dashboard))
        app.add_handler(CommandHandler("help", self.cmd_help))
        app.add_handler(CommandHandler("markets", self.cmd_markets))
        app.add_handler(CommandHandler("account", self.cmd_account))
        app.add_handler(CommandHandler("positions", self.cmd_positions))
        app.add_handler(CommandHandler("position", self.cmd_position))
        app.add_handler(CommandHandler("orders", self.cmd_orders))
        app.add_handler(CommandHandler("history", self.cmd_history))
        app.add_handler(CommandHandler("exposure", self.cmd_exposure))
        app.add_handler(CommandHandler("health", self.cmd_health))
        app.add_handler(CommandHandler("learning", self.cmd_learning))
        app.add_handler(CommandHandler("experiments", self.cmd_experiments))
        app.add_handler(CommandHandler("champion", self.cmd_champion))
        app.add_handler(CommandHandler("challengers", self.cmd_challengers))
        app.add_handler(CommandHandler("research", self.cmd_research))
        app.add_handler(CommandHandler("performance", self.cmd_performance))
        app.add_handler(CommandHandler("settings", self.cmd_settings))
        app.add_handler(CommandHandler("activity", self.cmd_activity))
        app.add_handler(CommandHandler("emergency", self.cmd_emergency))
        app.add_handler(CommandHandler("backtest", self.cmd_backtest))
        app.add_handler(CallbackQueryHandler(self.handle_callback))
        self.app = app
