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
from telegram.error import BadRequest
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
from analysis.objectives import ObjectiveInterpreter, ObjectivePreview, ObjectiveValidation, ObjectiveValidator, TradingObjective, phase_for_equity, objective_operational_readiness, resolve_requested_symbols
from analysis.objective_phases import plan_objective_phases
from bot.account_views import LiveAccountViews
from bot.capital_views import capital_actions_view, capital_test_view, demo_session_report_view
from risk.manager import RiskManager
from executors.mt5 import MT5Executor
from communication.command_bus import CommandBus, CommandRequest

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
        try:
            return await func(self, update, context, *args, **kwargs)
        except Exception as exc:
            command = getattr(getattr(update, "message", None), "text", None) or getattr(getattr(update, "callback_query", None), "data", None) or "command"
            logger.exception("Telegram command failed: %s", command)
            reply = update.callback_query.message if update.callback_query else update.message
            if reply:
                try:
                    await reply.reply_text(
                        "❌ **COMMAND ERROR**\n"
                        f"`{str(command)[:80]}` could not complete: `{type(exc).__name__}`.\n"
                        "The bot remains running. Use `/engine` for read-only diagnostics; the detailed traceback is in the VPS bot log."
                        , parse_mode="Markdown"
                    )
                except Exception:
                    logger.exception("Could not send Telegram command error reply")
            return None
    return wrapper


class BotHandlers:
    """Holds shared state for bot handlers."""

    def __init__(self, settings: TradeSettings, executor, risk_manager: RiskManager, scheduler=None, command_bus: CommandBus | None = None):
        self.settings = settings
        self._executor = executor
        self.risk_manager = risk_manager
        self.scheduler = scheduler
        self.command_bus = command_bus
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
        capital = await db.get_account_state("demo") if self.settings.trading_mode == "demo" else None
        capital_state = str((capital or {}).get("state") or "NOT YET VERIFIED")
        minimum_operating = float((capital or {}).get("minimum_operating_capital") or 0.0)
        capital_action = "RESET DEMO ACCOUNT" if capital_state == "CAPITAL_EXHAUSTED" else ("USE /resume AFTER VERIFIED RESET" if capital_state == "AWAITING_RESUME" else "MONITORING")
        active_objective = await db.get_active_objective(self.settings.trading_mode)
        if active_objective and not active_objective.get("is_paused"):
            operational = (active_objective.get("context") or {}).get("operational") or {}
            objective_line = f"Objective v{active_objective.get('version')}: FULL AUTO | {len(operational.get('allowed_symbols') or [])} operational instrument(s) | phase {operational.get('phase', 'UNAVAILABLE')}"
        elif active_objective:
            objective_line = f"Objective v{active_objective.get('version')}: OPERATIONAL PAUSE"
        else:
            objective_line = "Objective: no confirmed operational objective"
        return "\n".join([
            "🤖 **DERIV AUTONOMOUS RESEARCH SYSTEM**",
            f"Server Time: `{datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S')} UTC`",
            f"Mode: `{self.settings.trading_mode.upper()}` | Autonomous execution: `{'ON' if self.settings.auto_trade and not self.settings.is_paused else 'OFF'}`",
            f"Broker universe: `{active_count}` active / `{available_count}` available Deriv Synthetic Indices or Gold",
            f"Today: `{performance['trades']}` closed trades | P/L `${performance['pnl']:.2f}` | win rate `{performance['win_rate']:.1f}%`",
            f"Champion: `{model_text}` | Forward-DEMO challenger: `{experiment_text}`",
            f"Capital state: `{capital_state}` | Minimum operating capital: `${minimum_operating:.2f}` | Action: `{capital_action}`",
            f"Research engine: `{'enabled' if self.settings.self_optimization_enabled else 'disabled'}` — policy variables are learned from evidence, not fixed global caps.",
            f"{objective_line}",
            "\nUse the research controls below. LIVE always requires a separate explicit confirmation.",
        ])

    async def _objective_facts(self, *, refresh: bool) -> tuple[dict, str, tuple[str, ...]]:
        """Return broker/account facts for objective review; never executes a trade."""
        if self.scheduler:
            if refresh:
                await self.scheduler.refresh_market_universe()
                reconciliation = await self.scheduler.reconcile_account_state()
                capital = reconciliation.get("capital") or {}
            else:
                capital = self.scheduler.last_capital_state or {}
            account = dict(capital.get("account") or {})
            state = str(capital.get("state") or "ACCOUNT_STATE_UNKNOWN")
            # Discovery is distinct from immediate margin feasibility.  The
            # configured objective must resolve against the broker-approved
            # Deriv universe even when negative free margin makes no symbol
            # currently executable; the capital-state validator still blocks
            # confirmation and all new exposure until broker readiness returns.
            approved = tuple(record.symbol for record in self.scheduler.market_universe.accepted_records)
            return account, state, approved
        state_row = await db.get_account_state(self.settings.trading_mode)
        state = str((state_row or {}).get("state") or "ACCOUNT_STATE_UNKNOWN")
        account = {
            "balance": (state_row or {}).get("last_balance"),
            "equity": (state_row or {}).get("last_equity"),
            "free_margin": (state_row or {}).get("last_free_margin"),
            "currency": "USD",
        }
        return account, state, ()

    @staticmethod
    def _operational_objective_config(objective: TradingObjective, *, resolved_symbols: tuple[str, ...], broker_usable_symbols: tuple[str, ...], account: dict, phase: str, phase_plan: Optional[dict] = None) -> dict:
        explicit = bool(objective.requested_symbols)
        allowed = list(resolved_symbols) if explicit else list(broker_usable_symbols)
        return {
            "status": "ACTIVE", "account_mode": objective.account_mode,
            "starting_capital": objective.starting_capital if objective.starting_capital is not None else account.get("equity"),
            "target_capital": objective.target_capital,
            "growth_preference": objective.growth_preference,
            "capital_protection_preference": objective.capital_protection_preference,
            "allowed_symbols": allowed, "explicit_symbol_universe": explicit,
            "minimum_rr": objective.minimum_rr,
            "layering_preference": objective.layering_preference,
            "adaptive_sizing": objective.adaptive_sizing,
            "adaptive_management": objective.adaptive_management,
            "adaptive_learning": objective.adaptive_learning,
            "full_auto": True, "phase": phase, "phase_plan": dict(phase_plan or {}),
        }

    @staticmethod
    def _objective_plain_text(text: str) -> str:
        """Remove Markdown control characters for a Telegram-safe fallback."""
        return str(text).replace("**", "").replace("`", "").replace("_", "")

    @classmethod
    async def _reply_objective(cls, reply, text: str) -> None:
        """Reply with Objective Console Markdown, falling back to plain text.

        Broker status and persisted terminal reasons are dynamic. A malformed
        Markdown entity must never make the command appear non-responsive.
        """
        try:
            await reply.reply_text(text, parse_mode="Markdown")
        except BadRequest:
            logger.warning("Objective Console Markdown rejected by Telegram; sending plain-text fallback")
            await reply.reply_text(cls._objective_plain_text(text))

    @staticmethod
    def _format_objective_preview(preview: ObjectivePreview, *, heading: str = "🎯 **OBJECTIVE DRAFT**") -> str:
        objective = preview.objective
        validation = preview.validation
        account = preview.account_snapshot
        multiple = f"{objective.target_multiple:.2f}×" if objective.target_multiple is not None else "Not specified"
        rr = "Not specified" if objective.minimum_rr is None else str(objective.minimum_rr)
        usable = ", ".join(preview.broker_usable_symbols[:10]) or "None"
        lines = [
            heading, "",
            f"Starting capital: `${objective.starting_capital:,.2f}`" if objective.starting_capital is not None else "Starting capital: Not specified",
            f"Target capital: `${objective.target_capital:,.2f}`" if objective.target_capital is not None else "Target capital: Not specified",
            f"Target multiple: `{multiple}`",
            f"Growth preference: `{objective.growth_preference.upper()}` | Capital protection: `{objective.capital_protection_preference.upper()}`",
            f"Minimum RR: `{rr}` | Layering: `{objective.layering_preference.upper()}` | Mode: `FULL AUTO`",
            f"Adaptive sizing: `{'ON' if objective.adaptive_sizing else 'OFF'}` | Adaptive TP/SL: `{'ON' if objective.adaptive_management else 'OFF'}` | Learning: `{'ON' if objective.adaptive_learning else 'OFF'}`",
            f"Phase: `{preview.phase}` | Inherited account mode: `{objective.account_mode.upper()}`",
            "", "**Fresh broker evidence**",
            f"State: `{account.get('state') or 'current reconciliation'}` | Equity: `{account.get('currency') or 'USD'} {float(account.get('equity') or 0.0):,.2f}` | Free margin: `{account.get('currency') or 'USD'} {float(account.get('free_margin') or 0.0):,.2f}`",
            f"Broker-approved symbols: `{usable}`",
            "Resolved active instruments: " + (", ".join(f"{row.get('requested')} → {row.get('broker_symbol') or 'UNRESOLVED'} ({row.get('status')})" for row in preview.resolved_symbols) if preview.resolved_symbols else "Dynamic broker-verified Synthetic Indices / Gold universe"),
        ]
        if validation.errors:
            lines.extend(["", "**Errors — confirmation blocked**", *[f"❌ {item}" for item in validation.errors]])
        if validation.warnings:
            lines.extend(["", "**Warnings**", *[f"⚠️ {item}" for item in validation.warnings]])
        if validation.info:
            lines.extend(["", "**Information**", *[f"• {item}" for item in validation.info]])
        if not objective.requested_symbols:
            lines.extend(["", "⚠️ **Dynamic instrument scope** — no explicit instruments were named. The objective may use the current broker-verified Synthetic Indices / Gold universe. Create a new objective with `Trade only <instrument list>` to lock a strict allowlist."])
        lines.extend(["", "_This is an objective, not a guaranteed return. Existing broker and execution controls remain authoritative._"])
        return "\n".join(lines)

    @admin_only
    async def cmd_objective(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Manage a confirmed, versioned user objective without direct trading authority."""
        await self.reload_settings()
        args = list(context.args or [])
        action = args[0].lower() if args else "show"
        mode = self.settings.trading_mode
        reply = update.callback_query.message if update.callback_query else update.message
        if action in {"disable", "standalone", "off"}:
            changed = await db.set_objective_scope_disabled(mode, True)
            if changed and self.scheduler:
                self.scheduler._start_background_task("objective_scope_disable_scan", self.scheduler.activate_and_scan_now())
            await reply.reply_text(
                "🟡 Objective execution scope disabled. The existing DEMO scanner now operates on the fresh broker-verified Synthetic Index / Gold universe; ranking, broker validation, learning, and active-position protection remain enabled."
                if changed else "No active objective exists to disable."
            )
            return
        if action in {"enable", "on", "scope_on"}:
            changed = await db.set_objective_scope_disabled(mode, False)
            if changed and self.scheduler:
                self.scheduler._start_background_task("objective_scope_enable_scan", self.scheduler.activate_and_scan_now())
            await reply.reply_text(
                "🟢 Objective execution scope re-enabled. The confirmed objective is active again. An immediate refresh and scan has been started."
                if changed else "The objective could not be re-enabled; it may be terminal or no active objective exists."
            )
            return
        if action == "set":
            instruction = " ".join(args[1:]).strip()
            if not instruction:
                await reply.reply_text("Usage: `/objective set <natural-language objective>`", parse_mode="Markdown")
                return
            account, state, usable = await self._objective_facts(refresh=True)
            objective = ObjectiveInterpreter().parse(instruction, account_mode=mode)
            resolved, resolution = resolve_requested_symbols(objective.requested_symbols, usable)
            validation = ObjectiveValidator.validate(objective, account_snapshot=account, account_state=state, broker_usable_symbols=usable, resolved_symbols=resolution)
            phase = phase_for_equity(objective.starting_capital or account.get("equity"), account.get("equity"))
            preview = ObjectivePreview(objective, validation, {**account, "state": state}, usable, phase, resolution)
            operational = self._operational_objective_config(objective, resolved_symbols=resolved, broker_usable_symbols=usable, account=account, phase=phase)
            await db.create_objective_draft(
                account_mode=mode, raw_instruction=instruction, objective=objective.to_dict(), account_snapshot={**account, "state": state},
                broker_universe=list(usable), context={**preview.to_dict(), "operational": operational},
            )
            await self._reply_objective(reply, self._format_objective_preview(preview) + "\n\nUse `/objective confirm` to apply this valid draft or `/objective cancel` to discard it.")
            return
        if action == "confirm":
            draft = await db.get_objective_draft(mode)
            if not draft:
                # Confirmation writes the objective before persisting its phase
                # plan. If an older process failed in that narrow window, make
                # this command an idempotent recovery instead of forcing the
                # user to recreate a valid confirmed objective.
                active = await db.get_active_objective(mode)
                operational = dict(((active or {}).get("context") or {}).get("operational") or {})
                if active and not operational.get("terminal") and operational.get("session_status") == "AWAITING_START":
                    await self._reply_objective(
                        reply,
                        f"✅ **OBJECTIVE v{active.get('version')} SAVED**\n"
                        "The reusable objective template is already confirmed. Use `/objective start` after fresh broker validation to begin its first DEMO session."
                    )
                    return
                if active and not operational.get("terminal") and not operational.get("phase_plan") and self.scheduler:
                    recovered = await self.scheduler._ensure_objective_phase_plan(active)
                    recovered_operational = dict((recovered.get("context") or {}).get("operational") or {})
                    if recovered_operational.get("phase_plan"):
                        await self._reply_objective(
                            reply,
                            f"✅ **OBJECTIVE v{recovered.get('version')} RECOVERED**\n"
                            "A legacy interrupted activation was completed idempotently. Use `/objective` to inspect its current broker state."
                        )
                        return
                await reply.reply_text("No objective draft exists. Use `/objective set <instruction>` first.", parse_mode="Markdown")
                return
            account, state, usable = await self._objective_facts(refresh=True)
            objective = TradingObjective.from_dict(draft["objective"])
            # The configured account mode is authoritative; an instruction can never switch it.
            objective = TradingObjective.from_dict({**objective.to_dict(), "account_mode": mode})
            resolved, resolution = resolve_requested_symbols(objective.requested_symbols, usable)
            validation = ObjectiveValidator.validate(objective, account_snapshot=account, account_state=state, broker_usable_symbols=usable, resolved_symbols=resolution)
            phase = phase_for_equity(objective.starting_capital or account.get("equity"), account.get("equity"))
            preview = ObjectivePreview(objective, validation, {**account, "state": state}, usable, phase, resolution)
            operational = self._operational_objective_config(objective, resolved_symbols=resolved, broker_usable_symbols=usable, account=account, phase=phase)
            if not validation.valid:
                await self._reply_objective(reply, self._format_objective_preview(preview) + "\n\n❌ Objective was not activated.")
                return
            # Confirmation saves the reusable user objective as a durable
            # template. A separate explicit /objective start records fresh
            # broker equity and begins one reset-separated DEMO attempt.
            operational = self._operational_objective_config(
                objective, resolved_symbols=resolved, broker_usable_symbols=usable, account=account, phase=phase,
            )
            operational.update({"template_saved": True, "session_status": "AWAITING_START", "phase_plan": None})
            active = await db.confirm_objective_draft(
                mode, objective=objective.to_dict(), account_snapshot={**account, "state": state}, broker_universe=list(usable), context={**preview.to_dict(), "operational": operational},
            )
            await db.set_objective_paused(mode, True)
            self.settings.is_paused = True
            self.settings.automation_pause_reason = "OBJECTIVE_AWAITING_START"
            self.settings.self_optimization_enabled = bool(objective.adaptive_learning)
            await db.save_settings(self.settings)
            active_text = self._format_objective_preview(preview, heading=f"✅ **OBJECTIVE v{active['version']} SAVED**")
            active_text += "\n\n📌 **SAVED OBJECTIVE TEMPLATE**\nThe target, preferences, and resolved instrument scope are retained across bot restarts and completed or failed DEMO sessions.\n\nUse `/objective start` after fresh broker validation to begin a new reset-separated DEMO session."
            await self._reply_objective(reply, active_text)
            return
        if action == "start":
            if not self.scheduler:
                await reply.reply_text("Objective session start is unavailable because the scheduler is not initialized.")
                return
            started = await self.scheduler.start_saved_objective_session()
            if not started.get("started"):
                await reply.reply_text(
                    f"Objective session was not started: {started.get('reason') or 'fresh broker validation did not pass'}.\n"
                    "The saved objective remains unchanged; use `/objective` to view its retained configuration.",
                    parse_mode="Markdown",
                )
                return
            active = started["objective"]
            objective = TradingObjective.from_dict(active["objective"])
            phase = started["phase"]
            account = dict((started.get("capital") or {}).get("account") or {})
            instruments = ", ".join(started.get("instruments") or []) or "Dynamic broker-verified Synthetic Indices / Gold universe"
            self.scheduler._start_background_task("objective_session_start_scan", self.scheduler.activate_and_scan_now())
            await self._reply_objective(
                reply,
                "🚀 **NEW OBJECTIVE SESSION**\n"
                f"Session: `#{started['session_id']}` | Objective v{active.get('version')}\n\n"
                f"Saved objective: `${float(objective.starting_capital or 0.0):.2f}` → `${float(objective.target_capital or 0.0):.2f}`\n"
                f"Current DEMO equity: `${float(account.get('equity') or 0.0):.2f}`\n"
                f"Growth: `{objective.growth_preference.upper()}` | Protection: `{objective.capital_protection_preference.upper()}`\n"
                f"Allowed instruments: `{instruments}`\n\n"
                f"🎯 **PHASE {phase.get('session_phase_number') if phase.get('session_phase_number') is not None else 1} ACTIVE**\n"
                f"Milestone: `${float(phase.get('starting_equity') or 0.0):.2f}` → `${float(phase.get('target_equity') or 0.0):.2f}`\n"
                f"Adaptive plan: `{started.get('phase_count')}` milestones\n\n"
                "🟢 **ACTIVE — FULL AUTO DEMO**\nThe existing broker-validated scanner and independent position manager are active.",
            )
            return
        if action == "resume":
            active = await db.get_active_objective(mode)
            terminal = dict(((active or {}).get("context") or {}).get("operational") or {}).get("terminal")
            if terminal:
                await reply.reply_text(
                    "This objective is terminal and cannot be resumed. Its completed DEMO-session review is retained; create and confirm a new objective for the next research cycle."
                )
                return
            changed = await db.set_objective_paused(mode, False)
            if changed and self.scheduler and self.settings.auto_trade and not self.settings.is_paused:
                self.scheduler._start_background_task("objective_resume_scan", self.scheduler.activate_and_scan_now())
            await reply.reply_text("Operational objective resumed; the existing DEMO scanner is refreshing the confirmed objective universe." if changed else "No active objective exists to resume.")
            return
        if action == "cancel":
            cancelled = await db.cancel_objective_draft(mode)
            await reply.reply_text("Objective draft cancelled." if cancelled else "No objective draft was waiting for confirmation.")
            return
        if action == "history":
            active = await db.get_active_objective(mode)
            if not active:
                await reply.reply_text("No saved confirmed objective exists yet.")
                return
            rows = await db.get_objective_sessions(int(active["id"]))
            if not rows:
                await reply.reply_text("No reset-separated sessions have been started for this saved objective. Use `/objective start` after broker validation.", parse_mode="Markdown")
                return
            lines = [f"🎯 **OBJECTIVE SESSION HISTORY — v{active.get('version')}**", ""]
            for row in rows[:10]:
                lines.append(
                    f"Session #{row.get('id')} — `{str(row.get('status') or '').upper()}`\n"
                    f"`${float(row.get('start_equity') or 0.0):.2f}` → `${float(row.get('end_equity') or row.get('start_equity') or 0.0):.2f}` | "
                    f"trades `{row.get('strategy_trades', 0)}` | drawdown `{float(row.get('max_drawdown_pct') or 0.0):.1f}%`"
                )
            await self._reply_objective(reply, "\n\n".join(lines))
            return
        if action == "pause":
            changed = await db.set_objective_paused(mode, True)
            await reply.reply_text("Operational objective paused. New objective-scoped scanning is suspended; broker safety checks and current position management remain active." if changed else "No active objective exists to pause.")
            return
        active = await db.get_active_objective(mode)
        if action == "explain" and active:
            context_data = active.get("context") or {}
            operational_data = context_data.get("operational") or {}
            scope_disabled = bool(operational_data.get("scope_disabled"))
            await reply.reply_text(
                f"🧠 **ACTIVE OBJECTIVE — v{active.get('version')}**\n\nPhase: `{context_data.get('phase', 'UNAVAILABLE')}` | operational pause: `{'YES' if active.get('is_paused') else 'NO'}` | execution scope: `{'DISABLED — STANDALONE DEMO' if scope_disabled else 'ENABLED'}`\n"
                f"Mode: `FULL AUTO {active.get('account_mode', 'demo').upper()}`\nAllowed instruments: `{', '.join(operational_data.get('allowed_symbols') or []) or 'dynamic broker-verified universe'}`\n"
                + ("The saved objective is currently disabled as an execution scope; the existing standalone DEMO scanner uses the fresh broker-verified universe. Existing SMC, policy, sizing, broker margin, TP/SL, position-management, emergency-stop, and MT5 execution components remain the only mechanisms that decide and submit technically valid orders. Learning continues in the background."
                 if scope_disabled else "The confirmed objective controls the existing scanner and new-exposure allowlist. Existing SMC, policy, sizing, broker margin, TP/SL, position-management, emergency-stop, and MT5 execution components remain the only mechanisms that decide and submit technically valid orders. Learning continues in the background and does not wait before scanning."),
                parse_mode="Markdown",
            )
            return
        if not active:
            await reply.reply_text("No active objective. Use `/objective set <instruction>` to create a draft.", parse_mode="Markdown")
            return
        objective = TradingObjective.from_dict(active["objective"])
        validation_data = (active.get("context") or {}).get("validation") or {}
        validation = ObjectiveValidation(
            bool(validation_data.get("valid", True)), tuple(validation_data.get("errors") or ()),
            tuple(validation_data.get("warnings") or ()), tuple(validation_data.get("info") or ()),
        )
        current_account, current_state, current_usable = await self._objective_facts(refresh=False)
        display_account = {**(current_account or active.get("account_snapshot") or {}), "state": current_state}
        operational = (active.get("context") or {}).get("operational") or {}
        display_phase = phase_for_equity(operational.get("starting_capital") or objective.starting_capital, display_account.get("equity"))
        preview = ObjectivePreview(objective, validation, display_account, current_usable or tuple(active.get("broker_universe") or ()), display_phase, tuple((active.get("context") or {}).get("resolved_symbols") or ()))
        terminal = dict(operational.get("terminal") or {})
        readiness, readiness_detail = objective_operational_readiness(display_account, current_state, is_paused=bool(active.get("is_paused") or self.settings.is_paused))
        heading = f"✅ **OBJECTIVE v{active.get('version')} ACTIVE**"
        if terminal:
            heading = f"🏁 **OBJECTIVE v{active.get('version')} TERMINAL**"
            readiness_text = (
                f"🏁 **OBJECTIVE SESSION TERMINAL — {str(terminal.get('outcome') or 'recorded').upper()}**\n"
                f"Session: `#{terminal.get('demo_session_id', 'N/A')}` | Terminal equity: `${float(terminal.get('equity') or 0.0):.2f}`\n"
                f"Reason: {terminal.get('reason') or 'Broker-confirmed terminal outcome'}\n"
                "A one-time evidence review has been recorded. New exposure stays paused, while existing positions continue receiving broker-confirmed protection. After a broker-verified DEMO reset, use `/objective start` to reuse this saved objective for another session."
            )
        elif bool(operational.get("scope_disabled")):
            readiness_text = "🟡 **STANDALONE DEMO MODE — OBJECTIVE SCOPE DISABLED**\nThe saved objective remains available for later re-enabling, but it currently does not restrict the scanner or new-exposure universe. The existing broker-verified universe, evidence-aware ranking, broker validation, position management, and DEMO learning remain active. Use `/objective enable` to restore objective scope."
        elif readiness == "READY":
            readiness_text = "🟢 **FULL AUTO DEMO READY**\nScanner and automatic execution use this objective's operational universe, subject to the existing final execution gates."
        else:
            readiness_text = f"⛔ **FULL AUTO DEMO STANDBY — {readiness}**\n{readiness_detail}\nNo new objective-scoped order will be opened."
        await self._reply_objective(reply, self._format_objective_preview(preview, heading=heading) + "\n\n" + readiness_text)

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
            "`/brokercheck` — read-only MT5 price, volume, contract, and margin audit\n"
            "`/sizingtest <symbol>` — read-only full broker lot-size calculation for the latest rejected setup\n"
            "`/engine` — actual scheduler, scanner, analysis, execution, and task diagnostics\n"
            "`/positions` — active broker positions and recorded policy actions\n"
            "`/learning` — measured observations and next objective\n"
            "`/experiments` — immutable policy experiment lifecycle\n"
            "`/champion` — current validated policy and its evidence\n"
            "`/challengers` — independent candidates in forward DEMO\n"
            "`/research` — falsifiable hypotheses and candidate values\n"
            "`/performance` — fresh broker-source daily performance\n"
            "`/capital_test` — DEMO-only capital-reduction / capital-test status\n"
            "`/capital_target <equity> [tolerance]` — save an actual DEMO equity target\n"
            "`/capital_start` — show the deliberate-DEMO-drawdown confirmation prompt\n"
            "`/capital_pause`, `/capital_resume`, `/capital_cancel` — session controls\n"
            "`/capital_activity` — isolated intentional-reduction activity\n"
            "`/demo_session [id]` — reset-separated DEMO session report\n"
            "`/demo_auto_resume on|off` — optional verified-reset auto-resume\n"
            "`/backtest <symbol> <tf> <days>` — causal policy backtest with TP/SL replay evidence\n"
            "`/objective [set|confirm|start|cancel|history|explain|pause|disable|enable]` — saved objective template, scope control, and explicit DEMO session controls\n"
            "`/session` — current saved-objective DEMO session\n"
            "`/learned` — plain-language evidence summary across saved objective sessions\n"
            "`/knowledge` — persistent expert methodology hypotheses, tests, decisions, and uncertainties\n"
            "`/opportunities` — current ranked strategy, regime, evidence, and thesis board\n"
            "`/scan` — manually trigger the authoritative market scanner\n"
            "`/activity [detailed|essential|off]` — chart-study notification mode\n"
            "`/settings` — autonomy, alerts, and explicit DEMO/LIVE controls\n"
            "`/emergency` — pause new execution and optionally close positions\n\n"
            "Capital reduction is DEMO-only, requires an explicit confirmation, and uses direct MT5 broker-mode verification. Its isolated activity is excluded from strategy statistics and optimizer evidence. Trading-policy controls are intentionally not manual commands. Broker validity, synchronization, and emergency controls remain mandatory.",
        )

    async def cmd_bus_dispatch(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Adapt Telegram transport into the shared command bus."""
        if not self.command_bus or not update.effective_user or not update.effective_chat:
            await update.effective_message.reply_text("Command bus is unavailable; use /engine for diagnostics.")
            return
        request = CommandRequest(
            platform="telegram",
            user_id=str(update.effective_user.id),
            channel_id=str(update.effective_chat.id),
            text=update.effective_message.text or "",
        )
        response = await self.command_bus.dispatch(request)
        await update.effective_message.reply_text(response.text)

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
            "**Detailed** reports live scanning progress, closed-candle study, structure, hard-gate rejections, validated setups, final risk review, broker submission, execution, and management actions.",
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
    async def cmd_engine(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Show observed task lifecycle and process-lifetime engine telemetry without trading."""
        if not self.scheduler:
            await self._render_plain_menu(update, "ENGINE DIAGNOSTICS\n\nScheduler is unavailable; no runtime task evidence exists.")
            return
        runtime = self.scheduler.telemetry.snapshot(include_lifetime=True)
        lifetime = (runtime.get("lifetime") or {}).get("counters") or {}
        window = (runtime.get("window") or {})
        components = runtime.get("components") or {}
        tasks = self.scheduler.scheduled_task_status()
        scan_gate = dict(getattr(self.scheduler, "last_scan_gate", {}) or {})
        scan_disposition = dict(getattr(self.scheduler, "_last_scan_disposition", {}) or {})
        candle_purposes = dict((window.get("candle_purposes") or {}))
        management_reasons = dict((window.get("management_reasons") or {}))
        def status(name: str) -> str:
            component = components.get(name) or {}
            return f"{component.get('state', 'NOT_STARTED')} | reason: {component.get('reason') or 'none'} | last success: {component.get('last_success') or 'never'} | last failure: {component.get('last_failure') or 'none'}"
        lines = [
            "ENGINE DIAGNOSTICS — READ-ONLY",
            f"Process started: {runtime.get('started_at')}",
            "", "COMPONENTS",
            f"Market scanner: {status('market_scanner')}",
            f"Analysis engine: {status('analysis_engine')}",
            f"Execution engine: {status('execution_engine')}",
            f"Position manager: {status('position_manager')}",
            f"Learning engine: {status('learning_engine')}",
            f"Account reconciliation: {status('account_reconciliation')}",
            f"Capital management: {status('capital_management')}",
            "", "LATEST SCANNER GATE",
            f"State: `{scan_gate.get('state') or 'UNKNOWN'}` | Updated: `{scan_gate.get('updated_at') or 'never'}`",
            f"Reason: {scan_gate.get('reason') or 'No completed scan disposition is available.'}",
            f"Analysis symbols: `{int(scan_gate.get('analysis_symbols') or 0)}` | Broker-usable: `{scan_gate.get('broker_usable_symbols', 'UNKNOWN')}` | Objective/universe state: `{scan_gate.get('market_selection_state', 'UNKNOWN')}`",
            "", "LATEST SCAN WORK ACCOUNTING",
            f"Disposition: `{scan_disposition.get('state', 'UNKNOWN')}` | Reason: {scan_disposition.get('reason', 'none')}",
            f"Discovered/targeted/eligible: `{scan_disposition.get('symbols_discovered', 0)}/{scan_disposition.get('symbols_targeted', 0)}/{scan_disposition.get('symbols_eligible', 0)}`",
            f"Attempted/analyzed/rejected/failed: `{scan_disposition.get('symbols_attempted', 0)}/{scan_disposition.get('symbols_analyzed', 0)}/{scan_disposition.get('symbols_rejected', 0)}/{scan_disposition.get('symbols_failed', 0)}`",
            f"Candle purposes in current window: `{', '.join(f'{key}={value}' for key, value in sorted(candle_purposes.items())) or 'none'}`",
            "", "SCHEDULED TASKS",
        ]
        for task in tasks:
            lines.append(f"- {task.get('name')}: interval/trigger={task.get('interval') or task.get('trigger')}; first={task.get('first_started') or 'not yet'}; last run={task.get('last_started') or 'not yet'}; success={task.get('last_success') or 'never'}; failure={task.get('last_failure') or 'none'}; next={task.get('next_run') or 'not scheduled'}")
            if task.get("last_error"):
                lines.append(f"  error: {task['last_error']}")
        ranking = list(getattr(self.scheduler, "last_opportunity_ranking", []) or [])
        top_opportunity = ranking[0] if ranking else {}
        top_details = dict(top_opportunity.get("details") or {})
        lines.extend([
            "", "STRATEGY / OPPORTUNITY SELECTION",
            "Registry selection: ACTIVE for validated candidates; it ranks declared strategy families from existing setup observations, regime fit, and closed-trade evidence.",
            f"Strategies evaluated on current board: `{len({str((item.get('details') or {}).get('strategy') or '') for item in ranking if (item.get('details') or {}).get('strategy')})}` | Ranked opportunities: `{len(ranking)}`.",
            f"Top opportunity: `{top_opportunity.get('symbol') or 'none'}` | strategy `{top_details.get('strategy') or 'UNKNOWN'}` | regime `{top_details.get('regime') or 'UNKNOWN'}` | score `{float(top_opportunity.get('score') or 0.0):.1f}` | evidence `{top_details.get('confidence') or 'UNKNOWN'}`.",
            "", "PROCESS-LIFETIME COUNTERS",
            f"Scan cycles: {lifetime.get('scan_cycles_started', 0)} started / {lifetime.get('scan_cycles_completed', 0)} completed / {lifetime.get('scan_cycles_no_work', 0)} no-work / {lifetime.get('scan_cycles_failed', 0)} failed / {lifetime.get('scan_cycles_skipped_overlap', 0)} overlap-skipped",
            f"Symbols: {lifetime.get('symbols_attempted', 0)} attempted / {lifetime.get('symbols_analyzed', 0)} analyzed / {lifetime.get('symbols_rejected', 0)} rejected / {lifetime.get('symbols_failed', 0)} failed | Candles: {lifetime.get('candle_requests', 0)} requested / {lifetime.get('failed_candle_requests', 0)} failed",
            f"Candle purpose totals: analysis={lifetime.get('analysis_candle_requests', 0)} | execution={lifetime.get('execution_candle_requests', 0)} | position-management={lifetime.get('position_management_candle_requests', 0)}",
            f"Analysis: {lifetime.get('analysis_runs', 0)} runs / {lifetime.get('setups_detected', 0)} detected / {lifetime.get('setups_rejected', 0)} rejected",
            f"RR pipeline: {lifetime.get('setups_rr_checked', 0)} checked / {lifetime.get('setups_rr_passed', 0)} passed / {lifetime.get('setups_rr_rejected', 0)} rejected",
            f"Sizing pipeline: {lifetime.get('sizing_checked', 0)} checked / {lifetime.get('sizing_rejected', 0)} rejected | Margin: {lifetime.get('margin_checked', 0)} checked",
            f"Execution pipeline: {lifetime.get('execution_approved', 0)} approved / {lifetime.get('orders_submitted', 0)} submitted / {lifetime.get('orders_filled', 0)} filled / {lifetime.get('orders_rejected', 0)} rejected",
            f"Positions: {lifetime.get('positions_checked', 0)} checked / {lifetime.get('positions_requiring_action', 0)} requiring action / {lifetime.get('positions_modified', 0)} modified / {lifetime.get('positions_closed', 0)} closed",
            f"SL/TP modifications: {lifetime.get('sl_modifications', 0)} / {lifetime.get('tp_modifications', 0)} | Live observations: {lifetime.get('live_observations', 0)} | Completed observations: {lifetime.get('observations', 0)}",
            f"Current-window management reasons: `{', '.join(f'{key}={value}' for key, value in sorted(management_reasons.items())) or 'none'}`",
        ])
        await self._render_plain_menu(update, "\n".join(lines))

    @admin_only
    async def cmd_sizingtest(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Report the latest rejected setup's full sizing calculation without creating an order."""
        symbol = " ".join(context.args).strip() if context.args else "XAUUSDm"
        if not self.scheduler:
            await self._render_plain_menu(update, "SIZING TEST\n\nScheduler is unavailable; no broker-authoritative calculation can run.")
            return
        report = await self.scheduler.sizing_diagnostic(symbol)
        if not report.get("available"):
            await self._render_plain_menu(
                update,
                f"SIZING TEST — {symbol}\n\n{report.get('reason', 'No sizing evidence is available.')}\n\nNo order was submitted. Run this after a persisted sizing rejection.",
            )
            return
        account = report.get("account") or {}
        spec = report.get("broker_spec") or {}
        sizing = report.get("sizing") or {}
        min_probe = report.get("minimum_margin_probe") or {}
        calc_probe = report.get("calculated_margin_probe")
        currency = str(account.get("currency") or "USD")
        def number(value, digits=8):
            return "NOT CALCULATED" if value is None else f"{float(value):.{digits}g}"
        def money_probe(probe):
            if not probe:
                return "NOT CALCULATED"
            value = probe.get("margin")
            if value is None:
                return f"NOT CALCULATED ({probe.get('error') or 'MT5 returned no margin'})"
            return f"{currency} {float(value):,.2f}"
        reason_code = str(sizing.get("sizing_code") or "UNSPECIFIED")
        classification = {
            "MINIMUM_LOT_EXCEEDS_POLICY_RISK": "A + B — calculated volume is below broker minimum, and minimum-volume loss exceeds the active policy budget.",
            "MINIMUM_LOT_MARGIN_UNAFFORDABLE": "D — broker minimum volume requires more margin than available free margin.",
            "NO_STEP_NORMALIZED_LOT": "C — the calculated volume cannot be safely floored to the broker volume step at the broker minimum.",
            "BROKER_MARGIN_UNAVAILABLE": "E — MT5 margin evidence was unavailable; no margin assumption is used.",
            "BROKER_VOLUME_SPEC_INVALID": "F — the broker did not expose valid minimum-volume or step specifications.",
            "INVALID_STOP_SPEC": "H — entry/stop data cannot form a valid stop-loss distance.",
        }.get(reason_code, "H — see the exact sizing code and reason below.")
        history_note = "Historical policy inputs were persisted with this rejection." if report.get("historical_inputs_complete") else "Historical policy inputs were not present in this older rejection; current active policy was used for this read-only recomputation and is labelled non-historical."
        lines = [
            f"SIZING TEST — {symbol} — READ-ONLY",
            f"Rejected setup recorded: {report.get('recorded_at') or 'unknown'}",
            history_note,
            "", "ACCOUNT / POLICY",
            f"1. Account equity: {currency} {float(account.get('equity') or account.get('balance') or 0.0):,.2f}",
            f"   Effective capital: {currency} {float(report.get('effective_capital') or 0.0):,.2f}",
            f"2. Free margin: {currency} {float(account.get('free_margin') or 0.0):,.2f}",
            f"8. Configured risk: {float(report.get('risk_pct') or 0.0):.6g}% | Model: {report.get('risk_model')}",
            f"9. Maximum risk: {currency} {float(sizing.get('risk_amount') or 0.0):,.2f}",
            "", "SETUP",
            f"Direction: {report.get('direction')}",
            f"3. Entry: {number(report.get('entry_price'))}",
            f"4. Stop loss: {number(report.get('stop_loss'))}",
            f"5. Stop distance: {number(report.get('stop_distance'))}",
            f"6. Take profit: {number(report.get('take_profit'))}",
            f"7. RR: {number(report.get('rr_ratio'), 6)}",
            "", "BROKER SPECIFICATION",
            f"10. Minimum volume: {number(sizing.get('broker_min_lot'))}",
            f"11. Maximum volume: {number(sizing.get('broker_max_lot'))}",
            f"12. Volume step: {number(sizing.get('broker_volume_step'))}",
            f"13. Contract size: {number(spec.get('trade_contract_size') or spec.get('contract_size'))}",
            f"14. Tick size: {number(spec.get('trade_tick_size') or spec.get('tick_size'))}",
            f"15. Tick value: {number(spec.get('trade_tick_value') or spec.get('tick_value'))}",
            "", "LOT / LOSS / MARGIN CALCULATION",
            f"16. Theoretical policy-required volume: {number(sizing.get('policy_required_lot'))}",
            f"17. Broker-rounded executable volume: {number(sizing.get('required_lot'))}",
            f"18. Expected loss at broker minimum: {currency} {float(sizing.get('minimum_lot_loss') or 0.0):,.2f}",
            f"19. Expected loss at calculated executable volume: {currency} {float(sizing.get('expected_loss') or 0.0):,.2f}" if float(sizing.get('required_lot') or 0.0) > 0 else "19. Expected loss at calculated executable volume: NOT CALCULATED",
            f"20. Required margin at broker minimum: {money_probe(min_probe)}",
            f"21. Required margin at calculated executable volume: {money_probe(calc_probe)}",
            "", "DECISION",
            f"22. Code: {reason_code}",
            f"Exact reason: {sizing.get('reason') or report.get('historical_record', {}).get('reason') or 'No rejection reason recorded'}",
            f"Classification: {classification}",
            "", "No order was submitted. Broker volume and margin validation were not bypassed.",
        ]
        await self._render_plain_menu(update, "\n".join(lines))

    @admin_only
    async def cmd_brokercheck(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Run and display a read-only account-level MT5 executable-symbol audit."""
        if not self.scheduler:
            await self._render_plain_menu(update, "BROKER CHECK\n\nScheduler is unavailable, so no broker-authoritative metadata audit can be produced.")
            return
        await self.scheduler.refresh_market_universe()
        state_result = await self.scheduler.reconcile_account_state()
        capital = state_result.get("capital") or {}
        account = capital.get("account") or {}
        audit = capital.get("broker_metadata") or self.scheduler.capital_state_service.last_metadata_audit or {}
        try:
            json_path, markdown_path = self.scheduler.capital_state_service.write_metadata_audit("logs", audit, account)
            self.scheduler.last_broker_metadata_audit_paths = (str(json_path), str(markdown_path))
        except Exception as exc:
            json_path = markdown_path = None
            logger.exception("Could not write explicit broker-check audit: %s", exc)
        currency = str(account.get("currency") or "USD")
        current = bool(capital.get("current"))
        pipeline = audit.get("pipeline") or {}
        lines = [
            "🔎 BROKER CHECK — READ-ONLY MT5 AUDIT",
            f"MT5: {'CONNECTED' if current else 'UNAVAILABLE'}",
            f"Account: {str(account.get('broker_account_mode') or self.settings.trading_mode).upper()}",
            f"Balance: {currency} {float(account.get('balance') or 0.0):,.2f}",
            f"Equity: {currency} {float(account.get('equity') or 0.0):,.2f}",
            f"Free margin: {currency} {float(account.get('free_margin') or 0.0):,.2f}",
            f"Margin: {currency} {float(account.get('margin') or 0.0):,.2f} | Level: {float(account.get('margin_level') or 0.0):.1f}%",
            f"Account leverage: {account.get('leverage') if account.get('leverage') is not None else 'NOT EXPOSED'}",
            f"Broker symbols returned: {pipeline.get('broker_symbols_returned', 0)}",
            f"Synthetic targets detected: {pipeline.get('synthetic_targets_detected', 0)} | Gold detected: {pipeline.get('gold_targets_detected', 0)}",
            f"Broker-verified targets: {pipeline.get('broker_verified_targets', 0)} | Enabled targets: {pipeline.get('enabled_targets', audit.get('target_count', 0))}",
            f"Target symbols: {audit.get('target_count', 0)} | Passed metadata validation: {audit.get('usable_count', 0)} | Failed metadata validation: {audit.get('invalid_count', 0)}",
            f"Universe stage: {audit.get('universe_state', 'UNKNOWN')} | Account state: {capital.get('state') or 'ACCOUNT_STATE_UNKNOWN'}",
            f"Reason: {capital.get('reason') or 'Broker data unavailable'}",
            "",
            "SYMBOL VALIDATION",
        ]
        for record in audit.get("symbols", []):
            checks = record.get("checks") or {}
            status = "USABLE" if record.get("usable") else "INVALID"
            lines.extend([
                f"{record.get('symbol')} — {status}",
                f"  PRICE: {checks.get('price', 'NOT_EXPOSED')} | VOLUME: {checks.get('volume', 'NOT_EXPOSED')} | CONTRACT: {checks.get('contract', 'NOT_EXPOSED')}",
                f"  MARGIN: {checks.get('margin', 'NOT_EXPOSED')} ({record.get('margin_source') or 'NOT EXPOSED'}) | LEVERAGE: {checks.get('leverage', 'NOT_EXPOSED')}",
                f"  Required margin: {currency} {float(record.get('margin_required') or 0.0):,.2f} | {record.get('reason')}",
            ])
        if not audit.get("symbols"):
            if int(audit.get("target_count") or 0) == 0:
                lines.extend([
                    "⚠️ TARGET UNIVERSE EMPTY",
                    "The account validator received no completed broker-classified target symbols.",
                    "This is a broker universe/configuration/initialization pipeline condition, not a price, volume, or margin failure.",
                ])
            else:
                lines.append("No enabled broker target symbols were available for validation.")
        if markdown_path:
            lines.extend(["", f"Full audit: {markdown_path}", f"JSON audit: {json_path}"])
        report = "\n".join(lines)
        if len(report) <= 3900:
            await self._render_plain_menu(update, report)
            return
        # Never silently discard invalid-symbol reasons. Keep the main response
        # concise and deliver the remaining field-level audit in Telegram-safe
        # chunks to the same authorized chat.
        summary_end = lines.index("SYMBOL VALIDATION") + 1
        await self._render_plain_menu(update, "\n".join(lines[:summary_end] + ["Detailed per-symbol reasons follow in additional messages."]))
        detail = lines[summary_end:]
        chunk: list[str] = []
        size = 0
        for line in detail:
            needed = len(line) + 1
            if chunk and size + needed > 3500:
                await update.effective_chat.send_message("\n".join(chunk))
                chunk, size = [], 0
            chunk.append(line)
            size += needed
        if chunk:
            await update.effective_chat.send_message("\n".join(chunk))

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
        governance = getattr(self.scheduler, "last_research_governance", {}) if self.scheduler else {}
        market_selection = governance.get("market_selection") or {}
        strategy_rows = governance.get("top_strategies") or []
        cohort_text = ", ".join(market_selection.get("selected_symbols") or []) or "Awaiting fresh broker-universe ranking"
        disabled_count = len(market_selection.get("disabled_symbols") or [])
        strategy_text = " | ".join(
            f"#{row.get('rank')} {row.get('version')} ({row.get('evidence_stage')}, n={row.get('sample_size')}, E={float(row.get('expectancy_r') or 0):.2f}R)"
            for row in strategy_rows
        ) or "No ranked versioned policy evidence yet"
        text = [
            "🧠 **LEARNING STATUS**",
            f"Mode: `{mode.upper()}` | Active model: `{model_text}` | Latest governance decision: `{decision}`",
            f"Completed 30-day outcomes: `{performance['trades']}` | P/L `${performance['pnl']:.2f}` | win rate `{performance['win_rate']:.1f}%`",
            "",
            "**Current symbol evidence**",
            *(profile_lines or ["No in-memory profile yet. The next broker-candle scan will build observable profiles; completed outcomes are required before outcome statistics affect settings."]),
            "",
            "**Research Governance**",
            f"Execution cohort (max `{self.settings.research_market_limit}`): `{cohort_text}` | other broker-valid markets disabled for new strategy scans: `{disabled_count}`.",
            f"Top strategies (up to `{self.settings.strategy_ranking_limit}`): {strategy_text}.",
            "No-revenge rule: losses are evidence only. They cannot trigger immediate larger risk, additional trades/layers, or an intraday policy replacement.",
            "",
            "**Next objective**",
            f"Collect at least `{self.settings.optimization_min_sample_size}` completed DEMO R-recorded outcomes, then compare independently specified policies through train, validation, out-of-sample, and forward-DEMO evidence. Broker and software integrity remain mandatory; risk, RR, features, layering, and management are experimental.",
        ]
        await self._render_menu(update, "\n".join(text))

    @admin_only
    async def cmd_knowledge(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Show the supplied expert methodology as evidence-labelled hypotheses."""
        await db.ensure_expert_knowledge_seeded("demo")
        rows = await db.get_expert_knowledge_journal("demo", include_archived=False, limit=100)
        lines = [
            "🧪 EXPERT KNOWLEDGE / HYPOTHESIS JOURNAL",
            "",
            "The supplied SMC/ICT methodology is stored as prior knowledge. It is not a permanent rule set, does not force trades, and cannot authorize LIVE promotion.",
            "",
            "WHAT I WAS TAUGHT",
            f"{len(rows)} claims were recorded from the user-supplied expert methodology package.",
            "",
            "WHAT ACTUALLY WORKS",
        ]
        supported = [row for row in rows if str(row.get("decision")) == "SUPPORTED"]
        rejected = [row for row in rows if str(row.get("decision")) == "REJECTED"]
        testing = [row for row in rows if str(row.get("decision")) not in {"SUPPORTED", "REJECTED"}]
        if supported:
            for row in supported[:4]:
                lines.append(f"SUPPORTED — {row.get('hypothesis_key')}: n={int(row.get('sample_size') or 0)}, strength={row.get('evidence_strength') or 'UNKNOWN'}. {row.get('result') or ''}")
        else:
            lines.append("UNKNOWN — no claim currently has sufficient chronological forward-DEMO evidence for a supported conclusion.")
        lines.extend(["", "WHAT DOES NOT WORK"])
        if rejected:
            for row in rejected[:4]:
                lines.append(f"REJECTED — {row.get('hypothesis_key')}: n={int(row.get('sample_size') or 0)}. {row.get('result') or ''}")
        else:
            lines.append("UNKNOWN — no supplied claim has been rejected by sufficient evidence yet.")
        lines.extend(["", "WHAT I AM STILL TESTING"])
        if testing:
            for row in testing[:8]:
                lines.append(f"{row.get('hypothesis_key')} — {row.get('decision') or 'INCONCLUSIVE'} | {row.get('evidence_strength') or 'UNKNOWN'} | historical n={int(row.get('historical_sample_size') or 0)} | forward DEMO n={int(row.get('forward_sample_size') or 0)}")
        else:
            lines.append("No open hypothesis remains in the journal.")
        lines.extend(["", "WHAT I CURRENTLY PLAN TO USE"])
        planned = [row for row in rows if row.get("current_plan")]
        if planned:
            lines.extend(f"{row.get('hypothesis_key')}: {row.get('current_plan')}" for row in planned[:4])
        else:
            lines.append("Use existing broker-validated structure, strategy selection, and evidence governance; no expert claim is promoted as a fixed rule.")
        lines.extend(["", "WHAT I CURRENTLY PLAN TO AVOID"])
        lines.append("Avoid treating expert claims as proven, using a handful of trades as confirmation, look-ahead data, forced entries, or automatic LIVE promotion.")
        lines.extend(["", "WHAT WOULD CHANGE THE DECISION"])
        lines.append("A claim requires chronological historical/replay evidence plus forward-DEMO evidence with sample size, expectancy, uncertainty, MAE/MFE, drawdown, and instrument/regime/timeframe partitions. LIVE activation remains manual-only.")
        await self._render_plain_menu(update, "\n".join(lines))

    @admin_only
    async def cmd_learned(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Explain stored DEMO evidence in plain language without inventing conclusions."""
        mode = self.settings.trading_mode
        active = await db.get_active_objective(mode)
        if not active:
            await self._render_plain_menu(update, "🧠 WHAT I'VE LEARNED SO FAR\n\nUNKNOWN — no saved objective or session evidence exists yet.")
            return
        operational = dict((active.get("context") or {}).get("operational") or {})
        session_id = operational.get("demo_session_id")
        sessions = await db.get_objective_sessions(int(active["id"]))
        current = await db.get_demo_session_report(int(session_id)) if session_id else None
        symbol_rows = await db.get_demo_session_symbol_summary(int(session_id)) if session_id else []
        strategy_evidence = await db.get_strategy_evidence_summary(mode, days=90)
        lines = ["🧠 WHAT I'VE LEARNED SO FAR", ""]
        if current and int(current.get("strategy_trades") or 0) > 0:
            lines.append(f"CURRENT SESSION — observed `{current.get('strategy_trades', 0)}` completed strategy trades; `{current.get('wins', 0)}` wins and `{current.get('losses', 0)}` losses.")
            lines.append(f"Session expectancy: `{current.get('expectancy_r') if current.get('expectancy_r') is not None else 'UNKNOWN'}` R; drawdown: `{float(current.get('max_drawdown_pct') or 0.0):.1f}%`.")
        else:
            lines.append("CURRENT SESSION — UNKNOWN. There are not yet enough closed broker-confirmed trades to describe performance.")
        if symbol_rows:
            top = symbol_rows[0]
            sample = int(top.get("trades") or 0)
            stage = "VALIDATED" if sample >= self.settings.optimization_min_sample_size else "OBSERVED"
            lines.append(f"{stage}: `{top.get('symbol')}` currently has the strongest session evidence: `{sample}` completed trades and `${float(top.get('pnl') or 0.0):.2f}` realized P/L.")
            if len(symbol_rows) > 1:
                weak = symbol_rows[-1]
                lines.append(f"OBSERVED: `{weak.get('symbol')}` has the weakest current-session realized result: `${float(weak.get('pnl') or 0.0):.2f}` across `{int(weak.get('trades') or 0)}` trades.")
        else:
            lines.append("UNKNOWN: no instrument has enough closed session outcomes yet to be favored or avoided.")
        observed_strategy_rows = [row for row in strategy_evidence if int(row.get("sample_size") or 0) > 0]
        lines.extend(["", "STRATEGY EVIDENCE"])
        if observed_strategy_rows:
            for row in observed_strategy_rows[:3]:
                expectancy = row.get("expectancy_r")
                expectancy_text = "UNKNOWN" if expectancy is None else f"{float(expectancy):+.2f}R"
                lines.append(f"{row.get('confidence') or 'UNKNOWN'}: `{row.get('symbol')}` × `{row.get('strategy_id')}` in `{row.get('regime')}` ({row.get('timeframe')}) has n={int(row.get('sample_size') or 0)} and expectancy {expectancy_text}.")
            regime_totals: dict[tuple[str, str], dict] = {}
            for row in observed_strategy_rows:
                key = (str(row.get("regime") or "UNKNOWN"), str(row.get("strategy_id") or "UNKNOWN"))
                bucket = regime_totals.setdefault(key, {"n": 0, "weighted": 0.0})
                n = int(row.get("sample_size") or 0)
                bucket["n"] += n
                bucket["weighted"] += float(row.get("expectancy_r") or 0.0) * n
            best_regime, best_values = sorted(regime_totals.items(), key=lambda item: (-(item[1]["weighted"] / max(item[1]["n"], 1)), -item[1]["n"], item[0]))[0]
            regime_expectancy = best_values["weighted"] / max(best_values["n"], 1)
            lines.append(f"Best observed regime × strategy: `{best_regime[0]}` × `{best_regime[1]}` — n={best_values['n']}, weighted expectancy {regime_expectancy:+.2f}R. This remains a sample-labelled observation, not a guarantee.")
            layered = [row for row in observed_strategy_rows if row.get("strategy_id") == "layered_continuation"]
            if layered:
                layer_n = sum(int(row.get("sample_size") or 0) for row in layered)
                layer_e = sum(float(row.get("expectancy_r") or 0.0) * int(row.get("sample_size") or 0) for row in layered) / max(layer_n, 1)
                lines.append(f"Layering finding: layered continuation has n={layer_n} recorded completed outcomes with weighted expectancy {layer_e:+.2f}R. It remains eligible only when the original thesis, fresh confirmation, evidence, and broker checks all agree.")
            else:
                lines.append("Layering finding: UNKNOWN — no closed broker-confirmed layered-continuation outcome exists yet, so layering has no evidence advantage.")
        else:
            lines.append("UNKNOWN: no closed broker-confirmed strategy × instrument × regime outcome exists yet. Strategies are selected by current fit, but none is favored from outcome evidence.")
        ranking = list(getattr(self.scheduler, "last_opportunity_ranking", []) or []) if self.scheduler else []
        if ranking:
            best = ranking[0]
            lines.extend(["", "BEST CURRENT OPPORTUNITY"])
            details = dict(best.get("details") or {})
            lines.append(f"{best.get('symbol')} — {details.get('strategy') or 'UNKNOWN'} in {details.get('regime') or best.get('context', {}).get('regime', 'UNKNOWN')} | opportunity score {float(best.get('score') or 0.0):.1f} | evidence {details.get('confidence') or 'UNKNOWN'} (n={int(details.get('sample_size') or 0)}).")
            lines.append("Why: " + "; ".join(best.get("rationale") or ["current closed-candle and stored-evidence context is available"]) + ".")
        else:
            lines.append("CURRENT OPPORTUNITY — UNKNOWN. No current closed-candle ranking has produced a thesis-qualified candidate yet.")
        completed = [row for row in sessions if row.get("status") != "active"]
        lines.extend(["", "CURRENT PLAN"])
        lines.append("Doing: pursuing the saved objective through the current broker-validated instrument scope and the active experimental policy.")
        lines.append("Favoring: instruments and management responses with the strongest currently stored evidence; insufficient samples remain experimental.")
        lines.append("Avoiding: new exposure during broker margin pressure, repeated negative-evidence setups, and loss-driven size escalation.")
        lines.append("Testing: the active policy’s entry, sizing, layering, TP/SL, and capital-protection response variables through forward DEMO evidence.")
        lines.append("Uncertain: whether current results persist across additional phases and reset-separated DEMO sessions.")
        lines.append(f"Historical context: `{len(sessions)}` saved session(s), `{len(completed)}` completed/failed session(s).")
        await self._render_plain_menu(update, "\n".join(lines))

    @admin_only
    async def cmd_opportunities(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Show the latest descriptive opportunity board without submitting or altering orders."""
        ranking = list(getattr(self.scheduler, "last_opportunity_ranking", []) or []) if self.scheduler else []
        if not ranking:
            await self._render_plain_menu(
                update,
                "OPPORTUNITY BOARD — READ-ONLY\n\nNo current thesis-qualified candidate has been ranked yet. The next completed scan will analyze the broker-validated objective universe before selective execution.",
            )
            return
        lines = ["OPPORTUNITY BOARD — READ-ONLY", "", "Ranks are descriptive. Only the strongest current thesis may proceed to the existing final broker, sizing, portfolio, and execution checks."]
        for rank, item in enumerate(ranking[:10], start=1):
            details = dict(item.get("details") or {})
            expectancy = details.get("historical_expectancy_r")
            expectancy_text = "UNKNOWN" if expectancy is None else f"{float(expectancy):+.2f}R"
            conservative_ev = details.get("conservative_expected_value_r")
            conservative_ev_text = "UNKNOWN" if conservative_ev is None else f"{float(conservative_ev):+.2f}R"
            ci_low = details.get("expectancy_ci95_low_r")
            ci_high = details.get("expectancy_ci95_high_r")
            uncertainty_text = (
                f"[{float(ci_low):+.2f}R, {float(ci_high):+.2f}R]"
                if ci_low is not None and ci_high is not None else "UNAVAILABLE"
            )
            timeframe_summary = ", ".join(
                f"{entry.get('timeframe', 'TF')}={entry.get('bias', 'UNKNOWN')}"
                for entry in (details.get('htf_context') or [])
            ) or "UNKNOWN"
            lines.extend([
                "",
                f"{rank}. {item.get('symbol')} — {item.get('classification')}",
                f"Strategy: {details.get('strategy') or 'UNKNOWN'} | Evidence stage: {details.get('evidence_stage') or 'exploration'} | Regime: {details.get('regime') or item.get('context', {}).get('regime', 'UNKNOWN')} | Transition: {details.get('regime_transition') or 'UNKNOWN'} | Direction: {details.get('direction') or 'UNKNOWN'} | Timeframe: {details.get('timeframe') or 'UNKNOWN'}",
                f"Setup: {details.get('setup_status') or 'UNKNOWN'} | Research decision: {details.get('research_decision') or 'RESEARCH_ACCEPTED'} | Analysis gate: {details.get('analysis_trading_decision') or 'DEFERRED'} | Final state: {details.get('final_state') or 'PENDING_FINAL_VALIDATION'} | Final trading decision: {details.get('final_trading_decision') or 'PENDING_FINAL_VALIDATION'}",
                f"Evidence: {details.get('evidence_classification') or 'INSUFFICIENT'} | Completed confidence: {details.get('completed_confidence') or details.get('confidence') or 'UNKNOWN'} | n={int(details.get('sample_size') or 0)} | EV {details.get('expected_value_r') if details.get('expected_value_r') is not None else 'UNKNOWN'} | expectancy {expectancy_text} | conservative EV {conservative_ev_text} | uncertainty {details.get('uncertainty') or uncertainty_text}",
                f"Opportunity score: {float(item.get('score') or 0.0):.1f}/100 | Setup: {float(details.get('setup_score') or 0.0):.1f}/100 | Strategy fit: {float(details.get('strategy_score') or 0.0):.1f}/100 | Score authority: NON-AUTHORITATIVE",
                f"Top-down: {details.get('htf_bias_status') or 'UNKNOWN'} / {details.get('htf_relationship') or 'UNKNOWN'} | Timeframes: {timeframe_summary} | Observed features: {', '.join(details.get('observed_features') or []) or 'UNKNOWN'} | Displacement ratio: {float(details.get('displacement_ratio') or 0.0):.2f}",
                f"MAE {details.get('average_mae_r') if details.get('average_mae_r') is not None else 'UNKNOWN'}R | MFE {details.get('average_mfe_r') if details.get('average_mfe_r') is not None else 'UNKNOWN'}R | target-reach {details.get('target_reach_probability') if details.get('target_reach_probability') is not None else 'UNKNOWN'} | geometry present: {'YES' if details.get('execution_geometry_present') else 'NO'}",
                f"Entry {details.get('entry')} | SL {details.get('stop_loss')} | TP {details.get('take_profit')} | risk distance {details.get('risk_distance', 'UNKNOWN')} | reward distance {details.get('reward_distance', 'UNKNOWN')} | actual RR 1:{float(details.get('rr') or 0.0):.4f}",
                f"TP source: {details.get('target_source') or 'UNKNOWN'} | TP alternatives: {len(details.get('target_alternatives') or [])} | Target conflict: {'YES' if details.get('target_conflict') else 'NO'} | {details.get('target_reason') or 'No target conflict recorded'}",
                f"Layering evidence-supported: {'YES' if details.get('layering_suitability') else 'NO'} | Portfolio impact: {float(details.get('portfolio_conflict') or 0.0):.1f}",
                f"Confidence class: {details.get('confidence_classification') or 'UNKNOWN'} | Capacity eligible: {'YES' if details.get('capacity_allowed') else 'NO'} | Account state: {details.get('account_state') or 'UNKNOWN'} | Low capital: {'YES' if details.get('low_capital') else 'NO'}",
                f"Capacity reasons: {'; '.join(details.get('capacity_reasons') or ['none recorded'])} | Peer correlation: {details.get('maximum_peer_correlation') if details.get('maximum_peer_correlation') is not None else 'UNAVAILABLE'}",
                "Thesis: " + "; ".join(item.get("rationale") or ["no descriptive thesis available"]),
                "Final-gate reason: " + (details.get("final_trading_reason") or "Pending final broker, portfolio, sizing, stop, and execution validation."),
                "Learning objective: " + (details.get("learning_objective") or "Measure forward-DEMO outcome in this exact context."),
                "Selection: " + (details.get("why_selected") or details.get("why_not_selected") or "comparative explanation unavailable"),
            ])
        report = "\n".join(lines)
        if len(report) <= 3900:
            await self._render_plain_menu(update, report)
            return
        await self._render_plain_menu(update, "\n".join(lines[:4]))
        chunk: list[str] = []
        size = 0
        for line in lines[4:]:
            if chunk and size + len(line) + 1 > 3500:
                await update.effective_chat.send_message("\n".join(chunk))
                chunk, size = [], 0
            chunk.append(line)
            size += len(line) + 1
        if chunk:
            await update.effective_chat.send_message("\n".join(chunk))

    @admin_only
    async def cmd_session(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Show one readable status report for the current saved-objective session."""
        active = await db.get_active_objective(self.settings.trading_mode)
        operational = dict(((active or {}).get("context") or {}).get("operational") or {})
        session_id = operational.get("demo_session_id")
        if not active or not session_id:
            await self._render_plain_menu(update, "No active saved-objective session. Use `/objective start` after confirming a saved objective.")
            return
        session = await db.get_demo_session_report(int(session_id))
        phase = await db.get_active_objective_phase(int(active["id"]))
        if not session:
            await self._render_plain_menu(update, "Current objective session evidence is not available yet.")
            return
        objective = TradingObjective.from_dict(active["objective"])
        text = "\n".join([
            "📊 CURRENT OBJECTIVE SESSION",
            f"Session: #{session.get('id')} | Status: {str(session.get('status') or '').upper()}",
            f"Starting equity: ${float(session.get('start_equity') or 0.0):.2f} | Current equity: ${float(session.get('end_equity') or session.get('start_equity') or 0.0):.2f}",
            f"Saved target: ${float(objective.target_capital or 0.0):.2f}",
            f"Current phase: {phase.get('session_phase_number') if phase and phase.get('session_phase_number') is not None else 'N/A'} | Role: {operational.get('phase_role') or ('RECOVERY' if phase and phase.get('session_phase_number') == 0 else 'GROWTH')} | Target: ${float((phase or {}).get('target_equity') or 0.0):.2f}",
            f"Recovery target: ${float(operational.get('recovery_target_equity') or 0.0):.2f} | Previous phase: {operational.get('recovery_from_phase_number') if operational.get('recovery_from_phase_number') is not None else 'N/A'}",
            f"Trades: {session.get('strategy_trades', 0)} | Wins/losses: {session.get('wins', 0)}/{session.get('losses', 0)} | Drawdown: {float(session.get('max_drawdown_pct') or 0.0):.1f}%",
            f"Policy versions: {', '.join(session.get('policy_versions') or []) or 'baseline / pending closed evidence'}",
            "Learning status: broker-confirmed completed outcomes feed the existing governed optimizer.",
        ])
        await self._render_plain_menu(update, text)

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
        self.settings.automation_pause_reason = "EMERGENCY_STOP"
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
        if not self.scheduler:
            await reply_target.reply_text("Scheduler not initialized.")
            return

        # Check if a scan is already running to avoid unnecessary wait messages
        if self.scheduler._scan_lock.locked():
            await reply_target.reply_text("🔍 **SCAN IN PROGRESS**\nA market scan is already running. Please wait for it to complete.")
            return

        await reply_target.reply_text("🔍 **MANUAL SCAN INITIATED**\n_The bot is scanning the broker universe for high-probability setups..._")
        
        # Use the authoritative non-blocking scheduler implementation.
        # This ensures manual scans use the same timeout, lock, and execution
        # logic as the background loop.
        try:
            # Use a class flag to signal a manual scan bypass instead of 
            # changing the method signature, which is safer for background tasks.
            self.scheduler._manual_scan_requested = True
            asyncio.create_task(self.scheduler.scan_and_execute())
        except Exception as e:
            logger.error(f"Error initiating manual scan: {e}")
            await reply_target.reply_text(f"❌ Failed to start scan: {e}")

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

    def capital_engine(self):
        return getattr(self.scheduler, "capital_reduction", None) if self.scheduler else None

    @admin_only
    async def cmd_capital_test(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Show the isolated DEMO-only capital-reduction / capital-test state."""
        engine = self.capital_engine()
        account = await self.executor.get_account_info()
        session = await engine.status() if engine else None
        await self._render_plain_menu(
            update,
            capital_test_view(
                account=account, session=session,
                target=self.settings.capital_reduction_target,
                tolerance=self.settings.capital_reduction_tolerance,
            ),
            keyboards.capital_test_menu(bool(session and session.get("status") in {"active", "paused"})),
        )

    @admin_only
    async def cmd_capital_target(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Persist an operational DEMO target; no broker action is taken here."""
        if not context.args:
            await self._render_plain_menu(update, "Usage: /capital_target <target> [finish_abs] [finish_pct] [overshoot_abs] [overshoot_pct]\nExample: /capital_target 500 10 0 0 100")
            return
        try:
            target = float(context.args[0])
            tolerance = float(context.args[1]) if len(context.args) > 1 else float(self.settings.capital_reduction_tolerance)
            tolerance_pct = float(context.args[2]) if len(context.args) > 2 else float(self.settings.capital_reduction_tolerance_pct)
            overshoot = float(context.args[3]) if len(context.args) > 3 else float(self.settings.capital_reduction_overshoot_tolerance)
            overshoot_pct = float(context.args[4]) if len(context.args) > 4 else float(self.settings.capital_reduction_overshoot_tolerance_pct)
        except ValueError:
            await self._render_plain_menu(update, "Capital target values must be numeric. Example: /capital_target 500 10 0 0 100")
            return
        if target <= 0 or tolerance < 0 or tolerance_pct < 0 or overshoot < 0 or overshoot_pct < 0:
            await self._render_plain_menu(update, "Target must be positive and tolerance values cannot be negative.")
            return
        self.settings.capital_reduction_target = target
        self.settings.capital_reduction_tolerance = tolerance
        self.settings.capital_reduction_tolerance_pct = tolerance_pct
        self.settings.capital_reduction_overshoot_tolerance = overshoot
        self.settings.capital_reduction_overshoot_tolerance_pct = overshoot_pct
        await db.save_settings(self.settings)
        effective = max(tolerance, target * tolerance_pct / 100.0)
        effective_overshoot = max(overshoot, target * overshoot_pct / 100.0)
        await self._render_plain_menu(update, f"Aggressive DEMO reduction target saved: {target:,.2f}; finish tolerance ± {effective:,.2f}; overshoot envelope {effective_overshoot:,.2f} below target. No broker trade was placed. Use /capital_start to request the DEMO-only confirmation prompt.", keyboards.capital_test_menu())

    @admin_only
    async def cmd_capital_start(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Show the required explicit confirmation before creating a DEMO reduction session."""
        engine = self.capital_engine()
        if not engine:
            await self._render_plain_menu(update, "Capital-reduction engine is unavailable because the scheduler is not initialized.")
            return
        target = self.settings.capital_reduction_target
        if target is None:
            await self._render_plain_menu(update, "Set a target first: /capital_target <target> [finish_abs] [finish_pct] [overshoot_abs] [overshoot_pct]")
            return
        account = await self.executor.get_account_info()
        broker_mode = str((account or {}).get("broker_account_mode") or "unknown").lower()
        equity = float((account or {}).get("equity") or 0.0)
        if self.settings.trading_mode != "demo" or broker_mode != "demo":
            await self._render_plain_menu(update, f"CAPITAL REDUCTION BLOCKED\nConfigured mode: {self.settings.trading_mode.upper()} | MT5 broker mode: {broker_mode.upper()}\nThis operation is DEMO-only.")
            return
        if not account or target >= equity:
            await self._render_plain_menu(update, f"CAPITAL REDUCTION BLOCKED\nTarget must be below current actual DEMO equity. Current equity: {equity:,.2f}; target: {target:,.2f}.")
            return
        effective_tolerance = max(float(self.settings.capital_reduction_tolerance), float(target) * float(self.settings.capital_reduction_tolerance_pct) / 100.0)
        effective_overshoot = max(float(self.settings.capital_reduction_overshoot_tolerance), float(target) * float(self.settings.capital_reduction_overshoot_tolerance_pct) / 100.0)
        text = "\n".join([
            "⚠️ DELIBERATE DEMO DRAWDOWN",
            f"You are requesting aggressive deliberate reduction of the actual DEMO account toward {target:,.2f} ± {effective_tolerance:,.2f} finish tolerance, with up to {effective_overshoot:,.2f} permitted lower-bound overshoot.",
            f"Current actual MT5 equity: {equity:,.2f}",
            f"Maximum intended reduction before tolerance: approximately {equity - target:,.2f}",
            "This is DEMO-only. LIVE activation is blocked by both local mode and direct MT5 broker-mode verification.",
            "Continue?",
        ])
        if update.callback_query:
            await update.callback_query.edit_message_text(text, reply_markup=keyboards.confirm_keyboard("capital_reduction"), parse_mode=None)
        else:
            await update.message.reply_text(text, reply_markup=keyboards.confirm_keyboard("capital_reduction"), parse_mode=None)

    @admin_only
    async def cmd_capital_confirm(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Start the isolated session only after the explicit Telegram confirmation button."""
        engine = self.capital_engine()
        if not engine or self.settings.capital_reduction_target is None:
            await self._render_plain_menu(update, "Capital reduction cannot start: scheduler or target is unavailable.")
            return
        result = await engine.start(self.settings.capital_reduction_target, self.settings.capital_reduction_tolerance, self.settings.capital_reduction_tolerance_pct, self.settings.capital_reduction_overshoot_tolerance, self.settings.capital_reduction_overshoot_tolerance_pct)
        if not result.get("ok"):
            await self._render_plain_menu(update, f"CAPITAL REDUCTION NOT STARTED\n{result.get('reason', 'Unknown error')}", keyboards.capital_test_menu())
            return
        await self._render_plain_menu(update, "\n".join([
            "🔥 CAPITAL REDUCTION MODE ACTIVE",
            f"Session: #{result['session_id']}",
            f"Initial actual DEMO equity: {result['initial_equity']:,.2f}",
            f"Target equity: {result['target_equity']:,.2f} ± {max(result['tolerance'], result['target_equity'] * self.settings.capital_reduction_tolerance_pct / 100.0):,.2f} effective tolerance",
            "The scheduler will use only broker-verified DEMO data and isolated CAPITAL_REDUCTION activity records. These actions are excluded from strategy learning.",
        ]), keyboards.capital_test_menu(True))

    @admin_only
    async def cmd_capital_pause(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        engine = self.capital_engine()
        result = await engine.pause() if engine else {"ok": False, "reason": "Scheduler unavailable"}
        await self._render_plain_menu(update, "Capital reduction paused." if result.get("ok") else f"Capital reduction not paused: {result.get('reason')}", keyboards.capital_test_menu())

    @admin_only
    async def cmd_capital_resume(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        engine = self.capital_engine()
        result = await engine.resume() if engine else {"ok": False, "reason": "Scheduler unavailable"}
        await self._render_plain_menu(update, "Capital reduction resumed." if result.get("ok") else f"Capital reduction not resumed: {result.get('reason')}", keyboards.capital_test_menu())

    @admin_only
    async def cmd_capital_cancel(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        engine = self.capital_engine()
        result = await engine.cancel() if engine else {"ok": False, "reason": "Scheduler unavailable"}
        await self._render_plain_menu(update, "Capital reduction cancelled. No further reduction order will be generated." if result.get("ok") else f"Capital reduction not cancelled: {result.get('reason')}", keyboards.capital_test_menu())

    @admin_only
    async def cmd_capital_status(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        await self.cmd_capital_test(update, context)

    @admin_only
    async def cmd_demo_session_report(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Show one permanent reset-separated DEMO session report."""
        state = await db.get_account_state("demo")
        session_id = None
        if context.args:
            try:
                session_id = int(context.args[0])
            except ValueError:
                await self._render_plain_menu(update, "Usage: /demo_session [session_id]")
                return
        else:
            session_id = (state or {}).get("active_demo_session_id")
        if not session_id:
            await self._render_plain_menu(update, "No broker-verified DEMO session has been recorded yet.")
            return
        session = await db.get_demo_session_report(int(session_id))
        symbols = await db.get_demo_session_symbol_summary(int(session_id))
        account = await self.executor.get_account_info()
        currency = str((account or {}).get("currency") or "USD")
        await self._render_plain_menu(update, demo_session_report_view(session, symbols, currency))

    @admin_only
    async def cmd_capital_enter_test(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Report the automatic post-reduction transition; it never creates a broker order."""
        engine = self.capital_engine()
        session = await engine.status() if engine else None
        if not session or session.get("status") != "completed" or not session.get("capital_test_active"):
            await self._render_plain_menu(update, "CAPITAL-TEST MODE NOT READY\nComplete a DEMO capital-reduction session to the configured target first.", keyboards.capital_test_menu())
            return
        account = await self.executor.get_account_info()
        currency = str((account or {}).get("currency") or "USD")
        await self._render_plain_menu(update, "\n".join([
            "🎯 CAPITAL TEST READY",
            f"Starting DEMO equity: {session.get('initial_equity', 0.0):,.2f}",
            f"Target equity: {session.get('target_equity', 0.0):,.2f}",
            f"Actual current equity: {float((account or {}).get('equity') or 0.0):,.2f} {currency}",
            f"Actual current balance: {float((account or {}).get('balance') or 0.0):,.2f} {currency}",
            f"Current free margin: {float((account or {}).get('free_margin') or 0.0):,.2f} {currency}",
            f"Current margin level: {float((account or {}).get('margin_level') or 0.0):.1f}%",
            "Capital reduction is complete. Normal DEMO research now uses the actual broker account capital; no virtual balance is substituted.",
        ]), keyboards.capital_test_menu())

    @admin_only
    async def cmd_capital_activity(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        engine = self.capital_engine()
        session = await engine.status() if engine else None
        currency = str((await self.executor.get_account_info() or {}).get("currency") or "USD")
        await self._render_plain_menu(update, capital_actions_view(session, currency), keyboards.capital_test_menu(bool(session and session.get("status") in {"active", "paused"})))

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
        self.settings.automation_pause_reason = "MANUAL"
        await db.save_settings(self.settings)
        msg = "⏸ Auto-trading paused. Use /resume to continue."
        if update.callback_query:
            await update.callback_query.edit_message_text(msg, reply_markup=keyboards.main_menu())
        else:
            await update.message.reply_text(msg, reply_markup=keyboards.main_menu())

    @admin_only
    async def cmd_resume(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Resume only after a fresh viable broker account-capital verification."""
        service = getattr(self.scheduler, "capital_state_service", None) if self.scheduler else None
        result = await service.verify_resume() if service else {"resume_verified": False, "reason": "Scheduler/account-state service unavailable"}
        if not result.get("resume_verified"):
            msg = f"Trading remains HALTED. Broker account state is not viable for resumption: {result.get('reason', result.get('state', 'unknown'))}."
        else:
            self.settings.is_paused = False
            self.settings.automation_pause_reason = ""
            await db.save_settings(self.settings)
            if self.scheduler:
                scan_report = await self.scheduler.activate_and_scan_now()
                scan = dict(scan_report.get("scan") or {})
                disposition = dict(scan_report.get("disposition") or {})
                attempted = int(disposition.get("symbols_attempted") or 0)
                if not scan_report.get("ok"):
                    msg = (
                        "⚠️ DEMO broker state verified, but the immediate scan did not start.\n"
                        f"Reason: {scan.get('reason') or 'No verified broker instrument is available'}\n"
                        "Use /markets and /engine for the broker-universe diagnostic."
                    )
                elif scan.get("state") == "SKIPPED_OVERLAP":
                    msg = (
                        "ℹ️ DEMO broker state verified. The immediate scan was already running, "
                        "so no duplicate scan was started. Use /engine to monitor its result."
                    )
                elif attempted == 0:
                    msg = (
                        "⚠️ DEMO broker state verified, but the immediate scan completed with zero symbols attempted.\n"
                        f"State: `{disposition.get('state') or scan.get('state') or 'UNKNOWN'}`\n"
                        f"Reason: {disposition.get('reason') or scan.get('reason') or 'No scan work was available'}\n"
                        "Position management remains independent. Use /engine for the exact gate."
                    )
                else:
                    msg = (
                        "✅ DEMO broker state verified and immediate scan completed.\n"
                        f"State: `{disposition.get('state') or scan.get('state') or 'COMPLETED'}` | "
                        f"Symbols attempted: `{attempted}` | Analyzed: `{int(disposition.get('symbols_analyzed') or 0)}`\n"
                        "Any order still had to pass the existing research, portfolio, sizing, margin, stop, and broker-execution gates."
                    )
            else:
                msg = "⚠️ DEMO broker state verified, but the scheduler is unavailable; no immediate scan was started."
        if update.callback_query:
            await update.callback_query.edit_message_text(msg, reply_markup=keyboards.main_menu())
        else:
            await update.message.reply_text(msg, reply_markup=keyboards.main_menu())

    @admin_only
    async def cmd_demo_auto_resume(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Configure whether a broker-verified external DEMO reset may resume automatically."""
        if not context.args:
            await self._render_plain_menu(update, f"DEMO reset auto-resume: {'ON' if self.settings.demo_auto_resume_after_reset else 'OFF'}\nUse /demo_auto_resume on or /demo_auto_resume off.")
            return
        value = context.args[0].strip().lower()
        if value not in {"on", "off"}:
            await self._render_plain_menu(update, "Usage: /demo_auto_resume on or /demo_auto_resume off")
            return
        self.settings.demo_auto_resume_after_reset = value == "on"
        await db.save_settings(self.settings)
        await self._render_plain_menu(update, f"DEMO reset auto-resume is now {'ON' if self.settings.demo_auto_resume_after_reset else 'OFF'}. Broker reset verification remains mandatory.")

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
            f"Minimum RR: `{'DISABLED' if not self.settings.rr_filter_enabled else '1:' + format(self.settings.min_rr_ratio, 'g')}` | Preferred RR: `1:{self.settings.preferred_rr_ratio:g}`\n"
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
        """Request the evidence-governed daily research cycle."""
        await update.message.reply_text("🧠 **REQUESTING DAILY RESEARCH GOVERNANCE**\nReviewing chronological DEMO evidence; no loss-driven tuning will occur.")
        if self.scheduler:
            result = await self.scheduler.run_self_optimization()
            await update.message.reply_text(
                "🧠 **RESEARCH GOVERNANCE RESULT**\n"
                f"Decision: `{result.get('decision', 'unknown')}`\n"
                f"Reason: {result.get('reason', 'No additional detail recorded.')}"
            )
        else:
            await update.message.reply_text("❌ Scheduler not initialized.")

    @admin_only
    async def cmd_journal(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Show the daily AI journal."""
        if self.scheduler and self.scheduler.optimizer:
            journal = await self.scheduler.optimizer.generate_daily_journal(
                broker_usable_symbols=self.scheduler._analysis_eligible_symbols
            )
            await update.message.reply_text(journal, parse_mode="Markdown")
        else:
            await update.message.reply_text("❌ Optimizer not initialized.")

    @admin_only
    async def cmd_rr(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Configure minimum/preferred RR; zero disables only the RR filter."""
        args = list(context.args or [])
        if not args:
            minimum = "DISABLED" if not self.settings.rr_filter_enabled or self.settings.min_rr_ratio <= 0 else f"1:{self.settings.min_rr_ratio:g}"
            await update.message.reply_text(
                f"RR POLICY\nMinimum RR: {minimum}\nPreferred RR: 1:{self.settings.preferred_rr_ratio:g}\n"
                "Actual RR is always calculated and displayed.\nUsage: /rr 2 3 or /rr 0 to disable RR-only filtering."
            )
            return
        try:
            minimum = float(args[0])
            if minimum < 0:
                raise ValueError
            preferred = float(args[1]) if len(args) > 1 else self.settings.preferred_rr_ratio
            if preferred < 0:
                raise ValueError
        except ValueError:
            await update.message.reply_text("Usage: /rr 2 3 or /rr 0 to disable RR-only filtering")
            return
        self.settings.min_rr_ratio = minimum
        self.settings.rr_filter_enabled = minimum > 0.0
        self.settings.preferred_rr_ratio = preferred
        await db.save_settings(self.settings)
        minimum_text = "DISABLED" if not self.settings.rr_filter_enabled else f"1:{minimum:g}"
        await update.message.reply_text(
            f"✅ RR policy saved\nMinimum RR: {minimum_text}\nPreferred RR: 1:{preferred:g}\n"
            "Actual RR remains calculated. Structural targets remain authoritative; preferred RR does not manufacture TP."
        )

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
            from analysis.adaptive_management import observations_from_backtest, summarize_management
            management = summarize_management(observations_from_backtest(result))
            pf = management.get("profit_factor")
            pf_text = "N/A" if pf == float("inf") else f"{float(pf):.2f}"
            await update.message.reply_text(
                "🧠 **ADAPTIVE TP/SL REPLAY EVIDENCE**\n"
                f"Observations: `{management['sample_size']}` | expectancy: `{management['expectancy_r']:.2f}R` | PF: `{pf_text}`\n"
                f"MAE: `{management['average_mae_r']:.2f}R` | MFE: `{management['average_mfe_r']:.2f}R`\n"
                f"SL changes: `{management['sl_modifications']}` | TP changes: `{management['tp_modifications']}` | partial exits: `{management['partial_exits']}`\n"
                "This is causal backtest evidence only. It does not independently change the active execution policy."
            )
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
        elif data in {"capital_test", "capital_status"}:
            await self.cmd_capital_test(update, context)
        elif data == "capital_set_target":
            await self._render_plain_menu(update, "Set a target with: /capital_target <target_equity> [tolerance]\nExample: /capital_target 500 10", keyboards.capital_test_menu())
        elif data == "capital_start":
            await self.cmd_capital_start(update, context)
        elif data == "confirm_capital_reduction":
            await self.cmd_capital_confirm(update, context)
        elif data == "capital_pause":
            await self.cmd_capital_pause(update, context)
        elif data == "capital_resume":
            await self.cmd_capital_resume(update, context)
        elif data == "capital_cancel":
            await self.cmd_capital_cancel(update, context)
        elif data == "capital_activity":
            await self.cmd_capital_activity(update, context)
        elif data == "capital_enter_test":
            await self.cmd_capital_enter_test(update, context)
        elif data == "demo_session":
            await self.cmd_demo_session_report(update, context)
        elif data == "markets":
            await self.cmd_markets(update, context)
        elif data == "brokercheck":
            await self.cmd_brokercheck(update, context)
        elif data == "sizingtest":
            await self.cmd_sizingtest(update, context)
        elif data == "engine":
            await self.cmd_engine(update, context)
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
        elif data == "history":
            await self.cmd_history(update, context)
        elif data == "debug_mt5":
            await self.cmd_debug_mt5(update, context)
        elif data == "safety":
            streak = await db.get_consecutive_losses(account_mode=self.settings.trading_mode)
            await query.edit_message_text(
                "🛡 **Execution Safety Dashboard**\n\n"
                f"Entry: `{self.settings.entry_mode}` | Risk/setup: `{self.settings.risk_per_trade}%` (cap `{self.settings.max_setup_risk_pct}%`)\n"
                f"Open-risk ceiling: `{self.settings.max_total_open_risk_pct}%`\n"
                f"Daily stop: `-{self.settings.max_daily_loss_pct}%` / `+{self.settings.daily_profit_stop_pct}%`\n"
                f"Loss streak: `{streak}/{self.settings.max_consecutive_losses}`\n"
                f"Positions/layers: `{self.settings.max_open_positions}/{self.settings.max_layers}`\n"
                f"Min RR / preferred / quality: `{'DISABLED' if not self.settings.rr_filter_enabled else '1:' + format(self.settings.min_rr_ratio, 'g')}` / `1:{self.settings.preferred_rr_ratio:g}` / `{self.settings.min_setup_score}`\n"
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
                self.settings.automation_pause_reason = "AUTO_TRADE_MANUAL_OFF"
                await db.save_settings(self.settings)
                await query.edit_message_text(
                    "Auto-Trade is now OFF ❌",
                    reply_markup=keyboards.autotrade_menu(self.settings.auto_trade)
                )
        elif data == "confirm_autotrade_on":
            self.settings.auto_trade = True
            await db.save_settings(self.settings)
            if self.scheduler and not self.settings.is_paused:
                self.scheduler._start_background_task("activation_market_scan", self.scheduler.activate_and_scan_now())
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
                f"Current min RR: {'DISABLED' if not self.settings.rr_filter_enabled else '1:' + format(self.settings.min_rr_ratio, 'g')} | Preferred RR: 1:{self.settings.preferred_rr_ratio:g}\nUse command: /rr 2 3, or /rr 0 to disable filtering\n(Range: 0 or any positive values)",
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
        app.add_handler(CommandHandler("brokercheck", self.cmd_brokercheck))
        app.add_handler(CommandHandler("sizingtest", self.cmd_sizingtest))
        app.add_handler(CommandHandler("status", self.cmd_bus_dispatch))
        app.add_handler(CommandHandler("engine", self.cmd_bus_dispatch))
        app.add_handler(CommandHandler("account", self.cmd_account))
        app.add_handler(CommandHandler("positions", self.cmd_bus_dispatch))
        app.add_handler(CommandHandler("position", self.cmd_position))
        app.add_handler(CommandHandler("orders", self.cmd_orders))
        app.add_handler(CommandHandler("scan", self.cmd_scan))
        app.add_handler(CommandHandler("history", self.cmd_history))
        app.add_handler(CommandHandler("exposure", self.cmd_exposure))
        app.add_handler(CommandHandler("health", self.cmd_bus_dispatch))
        app.add_handler(CommandHandler("capital_test", self.cmd_capital_test))
        app.add_handler(CommandHandler("capital_target", self.cmd_capital_target))
        app.add_handler(CommandHandler("capital_start", self.cmd_capital_start))
        app.add_handler(CommandHandler("capital_pause", self.cmd_capital_pause))
        app.add_handler(CommandHandler("capital_resume", self.cmd_capital_resume))
        app.add_handler(CommandHandler("capital_cancel", self.cmd_capital_cancel))
        app.add_handler(CommandHandler("capital_status", self.cmd_capital_status))
        app.add_handler(CommandHandler("capital_activity", self.cmd_capital_activity))
        app.add_handler(CommandHandler("capital_enter_test", self.cmd_capital_enter_test))
        app.add_handler(CommandHandler("demo_session", self.cmd_demo_session_report))
        app.add_handler(CommandHandler("demo_auto_resume", self.cmd_demo_auto_resume))
        app.add_handler(CommandHandler("learning", self.cmd_learning))
        app.add_handler(CommandHandler("learned", self.cmd_learned))
        app.add_handler(CommandHandler("knowledge", self.cmd_knowledge))
        app.add_handler(CommandHandler("opportunities", self.cmd_bus_dispatch))
        app.add_handler(CommandHandler("session", self.cmd_session))
        app.add_handler(CommandHandler("experiments", self.cmd_experiments))
        app.add_handler(CommandHandler("champion", self.cmd_champion))
        app.add_handler(CommandHandler("challengers", self.cmd_challengers))
        app.add_handler(CommandHandler("research", self.cmd_research))
        app.add_handler(CommandHandler("performance", self.cmd_performance))
        app.add_handler(CommandHandler("settings", self.cmd_settings))
        app.add_handler(CommandHandler("pause", self.cmd_bus_dispatch))
        app.add_handler(CommandHandler("resume", self.cmd_bus_dispatch))
        app.add_handler(CommandHandler("confirm", self.cmd_bus_dispatch))
        app.add_handler(CommandHandler("activity", self.cmd_activity))
        app.add_handler(CommandHandler("emergency", self.cmd_emergency))
        app.add_handler(CommandHandler("backtest", self.cmd_backtest))
        app.add_handler(CommandHandler("objective", self.cmd_objective))
        app.add_handler(CallbackQueryHandler(self.handle_callback))
        self.app = app
