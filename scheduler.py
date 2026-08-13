"""
Market scanner & auto-execution scheduler.

Periodically scans configured symbols, runs full APA + S/D analysis,
scores signals, and auto-executes trades that pass all risk gates.
"""

from __future__ import annotations

import asyncio
import logging
import json
import os
from time import monotonic, perf_counter
from typing import Optional
from datetime import datetime, timedelta

import pandas as pd
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.interval import IntervalTrigger

from config import TradeSettings
from storage import db
from analysis.structure import analyze_structure, MarketStructure, Trend
from analysis.supply_demand import detect_sd_zones, SupplyDemandZone, ZoneType
from analysis.scoring import TradeSignal, format_signal_report
from analysis.indicators import pip_value, atr
from strategy.setup_scorer import score_setup_quality
from strategy.setup_validator import EntryMode, SetupValidator
from analysis.sessions import check_trading_session
from analysis.confirmation import get_confirmation
from analysis.liquidity import build_liquidity_pools, select_market_target
from analysis.visuals import render_smc_chart
from execution.manager import ManagementState, TradeManager
from execution.capital_reduction import CapitalReductionEngine
from analysis.profiler import profiler
from risk.manager import RiskManager
from executors.base import BaseExecutor, ExecutionResult
from analysis.optimizer import SelfOptimizer
from analysis.account_monitor import AccountReconciliationEngine
from analysis.capital_state import AccountCapitalState, CapitalStateService
from analysis.runtime_telemetry import RuntimeTelemetry
from data.provider import DataProvider
from data.universe import DerivMarketUniverse

logger = logging.getLogger(__name__)

# MT5 timeframe mapping
TIMEFRAME_MAP = {
    "M1": 1, "M5": 5, "M15": 15, "M30": 30,
    "H1": 60, "H4": 240, "D1": 1440, "W1": 10080,
}


class MarketScheduler:
    """Scans markets and auto-executes trades."""

    def __init__(
        self,
        settings: TradeSettings,
        executor: BaseExecutor,
        risk_manager: RiskManager,
        bot_app=None,
        admin_chat_id: Optional[int] = None,
    ):
        self.settings = settings
        self.executor = executor
        self.risk_manager = risk_manager
        self.bot_app = bot_app
        self.admin_chat_id = admin_chat_id
        self.scheduler = AsyncIOScheduler()
        self._running = False
        self.data_provider = DataProvider(self.executor)
        self.market_universe = DerivMarketUniverse()
        self.last_universe_audit_paths: tuple[str, str] | None = None
        # In-memory delivery ledger: a chart-stage alert is sent once per
        # closed candle/fingerprint and is throttled independently per symbol.
        self._chart_activity_ledger: dict[str, tuple[str, float]] = {}
        # Initialize Self-Optimizer
        self.optimizer = SelfOptimizer(self.settings)
        self.account_reconciliation = AccountReconciliationEngine(self.executor, self.settings.trading_mode)
        self.last_account_reconciliation: dict = {}
        self.capital_state_service = CapitalStateService(self.settings, self.executor)
        self.capital_state_service.begin_target_universe_refresh({"stage": "scheduler_created"})
        self.last_capital_state: dict = {}
        self.last_broker_metadata_audit_paths: tuple[str, str] | None = None
        self.capital_reduction = CapitalReductionEngine(self.settings, self.executor)
        self.telemetry = RuntimeTelemetry()
        # This is populated only from the fresh capital-service broker audit.
        # It is intentionally separate from settings because settings reloads
        # discard broker symbol lists to prevent stale post-restart execution.
        self._analysis_eligible_symbols: tuple[str, ...] = ()
        # One full scan can be lengthy across a broker-verified universe. All
        # triggers share this guard, preventing duplicate analysis or execution.
        self._scan_lock = asyncio.Lock()

    def _set_analysis_eligible_symbols(self, audit: Optional[dict]) -> tuple[str, ...]:
        audit = audit or {}
        usable = tuple(sorted({str(symbol).strip() for symbol in (audit.get("usable_symbols") or []) if str(symbol).strip()}))
        self._analysis_eligible_symbols = usable
        logger.info("[ANALYSIS ELIGIBILITY] broker_usable=%s symbols=%s", len(usable), list(usable))
        return usable

    def _analysis_symbol_is_eligible(self, symbol: str) -> bool:
        return str(symbol) in self._analysis_eligible_symbols

    async def _run_scheduled_task(self, name: str, interval: str, callback):
        """Record real scheduled-task entry/outcome and never discard exceptions."""
        self.telemetry.task_started(name, interval=interval)
        try:
            result = await callback()
        except Exception as exc:
            self.telemetry.task_failed(name, exc)
            logger.exception("Scheduled task %s failed", name)
            if name == "market_scan":
                await self._notify(
                    f"🔴 **SCANNER FAILURE**\\nTask: `{name}`\\nError: `{type(exc).__name__}: {exc}`\\n"
                    f"Last successful scan: `{self.telemetry.snapshot().get('components', {}).get('market_scanner', {}).get('last_success') or 'never'}`"
                )
            raise
        self.telemetry.task_succeeded(name)
        return result

    async def _market_scan_job(self):
        return await self._run_scheduled_task("market_scan", "configured scan interval", self.scan_and_execute)

    async def _universe_refresh_job(self):
        return await self._run_scheduled_task("market_universe_refresh", "1 hour", self.refresh_market_universe)

    async def _account_reconciliation_job(self):
        return await self._run_scheduled_task("account_reconciliation", "5 minutes", self.reconcile_account_state)

    async def _capital_reduction_job(self):
        return await self._run_scheduled_task("capital_reduction", "15 seconds", self.run_capital_reduction)

    async def _heartbeat_job(self):
        return await self._run_scheduled_task("activity_heartbeat", "10 minutes", self.send_activity_heartbeat)

    async def _optimization_job(self):
        return await self._run_scheduled_task("self_optimization", "daily", self.run_self_optimization)

    async def _daily_journal_job(self):
        return await self._run_scheduled_task("daily_journal", "daily", self.send_daily_journal)

    def _observe_background_task(self, name: str, task: asyncio.Task) -> None:
        """Consume and report every unexpected exception from create_task work."""
        try:
            task.result()
        except asyncio.CancelledError:
            logger.info("Background task %s was cancelled", name)
        except Exception as exc:
            self.telemetry.task_failed(name, exc)
            logger.exception("Background task %s failed", name)
        else:
            self.telemetry.task_succeeded(name)

    def _start_background_task(self, name: str, coroutine) -> asyncio.Task:
        self.telemetry.task_started(name, interval="immediate")
        task = asyncio.create_task(coroutine, name=name)
        task.add_done_callback(lambda completed: self._observe_background_task(name, completed))
        return task

    def scheduled_task_status(self) -> list[dict]:
        """Merge real APScheduler next-runs with observed task entry/outcome data."""
        observed = self.telemetry.snapshot().get("tasks", {})
        rows = []
        for job in self.scheduler.get_jobs():
            row = dict(observed.get(job.id, {"name": job.id}))
            row.update({
                "name": job.id,
                "next_run": job.next_run_time.isoformat() if job.next_run_time else None,
                "trigger": str(job.trigger),
            })
            rows.append(row)
        return sorted(rows, key=lambda item: item["name"])

    async def start(self, interval_seconds: int = 300):
        """Start the periodic market scanner after broker-market discovery."""
        broker_ready = await self.data_provider.init()
        universe_ready = await self.refresh_market_universe()
        if not broker_ready or not universe_ready:
            logger.error("Market scans are fail-closed until Deriv symbol discovery succeeds")

        self.scheduler.add_job(
            self._market_scan_job,
            IntervalTrigger(seconds=interval_seconds),
            id="market_scan",
            replace_existing=True,
        )
        self.scheduler.add_job(
            self._universe_refresh_job,
            IntervalTrigger(hours=1),
            id="market_universe_refresh",
            replace_existing=True,
        )
        self.scheduler.add_job(
            self._account_reconciliation_job,
            IntervalTrigger(minutes=5),
            id="account_reconciliation",
            replace_existing=True,
        )
        self.scheduler.add_job(
            self._capital_reduction_job,
            IntervalTrigger(seconds=15),
            id="capital_reduction",
            replace_existing=True,
        )
        self.scheduler.add_job(
            self._heartbeat_job,
            IntervalTrigger(minutes=10),
            id="activity_heartbeat",
            replace_existing=True,
        )
        self.scheduler.start()
        self._running = True
        logger.info(f"Market scanner started (every {interval_seconds}s)")
        
        # Run the bounded optimizer before the daily report. The optimizer can
        # also record an explicit no-change decision when evidence is weak.
        from apscheduler.triggers.cron import CronTrigger
        optimization_hour = (self.settings.daily_report_hour_utc - 1) % 24
        self.scheduler.add_job(
            self._optimization_job,
            CronTrigger(hour=optimization_hour, minute=self.settings.daily_report_minute_utc),
            id="self_optimization",
            replace_existing=True,
        )
        self.scheduler.add_job(
            self._daily_journal_job,
            CronTrigger(hour=self.settings.daily_report_hour_utc, minute=self.settings.daily_report_minute_utc),
            id="daily_journal",
            replace_existing=True,
        )
        for job in self.scheduler.get_jobs():
            logger.info("[TASK SCHEDULED] name=%s trigger=%s next_run=%s", job.id, job.trigger, job.next_run_time)

        # Establish a broker-authoritative capital state before the first scan.
        await self.reconcile_account_state()
        # Force an immediate scan on startup only after that verification.
        self._start_background_task("startup_market_scan", self.scan_and_execute())

    async def reconcile_account_state(self) -> dict:
        """Run the authoritative broker state and account reconciliation with telemetry."""
        self.telemetry.component_started("account_reconciliation")
        try:
            result = await self._reconcile_account_state()
        except Exception as exc:
            self.telemetry.component_failed("account_reconciliation", exc)
            raise
        self.telemetry.component_succeeded("account_reconciliation", waiting=False)
        return result

    async def _reconcile_account_state(self) -> dict:
        """Run the one authoritative broker capital-state evaluation plus read-only trade reconciliation."""
        self.capital_state_service.settings = self.settings
        self.capital_state_service.executor = self.executor
        capital = await self.capital_state_service.evaluate()
        self.last_capital_state = capital
        audit = capital.get("broker_metadata") or {}
        self._set_analysis_eligible_symbols(audit)
        logger.info("[ACCOUNT VALIDATOR] Received targets: %s | Usable: %s | Invalid: %s | State: %s", audit.get("target_count", 0), audit.get("usable_count", 0), audit.get("invalid_count", 0), capital.get("state"))
        self.account_reconciliation.executor = self.executor
        self.account_reconciliation.account_mode = self.settings.trading_mode
        snapshot = await self.account_reconciliation.snapshot(history_days=0)
        result = await self.account_reconciliation.reconcile(snapshot)
        self.last_account_reconciliation = result

        state = capital.get("state")
        blocking = state in AccountCapitalState.BLOCKING or state == AccountCapitalState.CAPITAL_EXHAUSTED
        if blocking and not self.settings.is_paused:
            self.settings.is_paused = True
            await db.save_settings(self.settings)
            logger.warning("New trading halted by authoritative account state: %s (%s)", state, capital.get("reason"))
        elif capital.get("reset_detected") and self.settings.demo_auto_resume_after_reset:
            verified = await self.capital_state_service.verify_resume()
            self.last_capital_state = verified
            if verified.get("resume_verified"):
                self.settings.is_paused = False
                await db.save_settings(self.settings)
        elif (
            self.settings.is_paused and self.settings.auto_trade
            and str((capital.get("previous") or {}).get("state") or "") in {
                AccountCapitalState.ACCOUNT_STATE_UNKNOWN, AccountCapitalState.TARGET_UNIVERSE_INITIALIZING,
                AccountCapitalState.TARGET_UNIVERSE_EMPTY, AccountCapitalState.TARGET_SYMBOLS_VALIDATING,
                AccountCapitalState.TARGET_SYMBOLS_INVALID,
            }
            and state in {AccountCapitalState.ACCOUNT_VERIFIED, AccountCapitalState.LOW_CAPITAL}
        ):
            # This is not an account reset. It is a recovery from a fail-closed
            # metadata/connection unknown state after current MT5 facts prove an
            # executable target. Reset transitions remain separately controlled.
            self.settings.is_paused = False
            await db.save_settings(self.settings)
            logger.info("Autonomous DEMO scanning resumed after broker metadata verification")

        if capital.get("changed"):
            audit = capital.get("broker_metadata")
            if audit:
                try:
                    json_path, markdown_path = self.capital_state_service.write_metadata_audit("logs", audit, capital.get("account"))
                    self.last_broker_metadata_audit_paths = (str(json_path), str(markdown_path))
                    logger.info("MT5 broker metadata audit written: %s", markdown_path)
                except Exception as exc:
                    logger.exception("Could not write broker metadata audit: %s", exc)
            await self._notify_capital_state(capital)
        if not result.get("current"):
            logger.warning("Account reconciliation unavailable: %s", result.get("error"))
        elif result.get("discrepancies"):
            logger.warning("Account reconciliation found %s discrepancy(s): %s", len(result["discrepancies"]), result["discrepancies"][:3])
        else:
            logger.info("Account reconciliation is synchronized: %s MT5 positions", result.get("broker_open_positions", 0))
        return {"capital": capital, "reconciliation": result}

    async def _notify_capital_state(self, capital: dict) -> None:
        """Send exactly one alert per material capital-state transition."""
        if not self.bot_app or not self.admin_chat_id:
            return
        account = capital.get("account") or {}
        currency = str(account.get("currency") or "USD")
        state = str(capital.get("state") or "ACCOUNT_STATE_UNKNOWN")
        minimum = float(capital.get("minimum_operating_capital") or 0.0)
        if state == AccountCapitalState.CAPITAL_EXHAUSTED:
            text = "\n".join([
                "🚨 DEMO CAPITAL EXHAUSTED",
                "The bot can no longer reliably open a broker-valid minimum position with the actual account state.",
                f"Balance: {currency} {float(account.get('balance') or 0.0):,.2f}",
                f"Equity: {currency} {float(account.get('equity') or 0.0):,.2f}",
                f"Free margin: {currency} {float(account.get('free_margin') or 0.0):,.2f}",
                f"Margin level: {float(account.get('margin_level') or 0.0):.1f}%",
                f"Minimum operating capital: {currency} {minimum:,.2f}",
                f"Reason: {capital.get('reason')}",
                "Status: TRADING HALTED. Reset the Deriv DEMO account externally, then use /health and /resume after the reset is verified.",
            ])
        elif state == AccountCapitalState.AWAITING_RESUME:
            previous = capital.get("previous") or {}
            text = "\n".join([
                "✅ DEMO ACCOUNT RESET DETECTED",
                f"Previous balance: {currency} {float(previous.get('last_balance') or 0.0):,.2f}",
                f"New balance: {currency} {float(account.get('balance') or 0.0):,.2f}",
                f"New equity: {currency} {float(account.get('equity') or 0.0):,.2f}",
                f"Free margin: {currency} {float(account.get('free_margin') or 0.0):,.2f}",
                "Trading remains PAUSED. Use /resume to re-verify the broker state and resume DEMO trading.",
            ])
        elif state in {AccountCapitalState.TARGET_UNIVERSE_INITIALIZING, AccountCapitalState.TARGET_UNIVERSE_EMPTY, AccountCapitalState.TARGET_SYMBOLS_VALIDATING, AccountCapitalState.TARGET_SYMBOLS_INVALID}:
            audit = capital.get("broker_metadata") or {}
            pipeline = audit.get("pipeline") or {}
            headline = {
                AccountCapitalState.TARGET_UNIVERSE_INITIALIZING: "⏳ TARGET UNIVERSE INITIALIZING",
                AccountCapitalState.TARGET_UNIVERSE_EMPTY: "⚠️ TARGET UNIVERSE EMPTY",
                AccountCapitalState.TARGET_SYMBOLS_VALIDATING: "⏳ TARGET SYMBOLS VALIDATING",
                AccountCapitalState.TARGET_SYMBOLS_INVALID: "⚠️ TARGET SYMBOLS INVALID",
            }.get(state, "⚠️ TARGET UNIVERSE HALTED")
            text = "\n".join([
                headline,
                "Trading is halted while broker target discovery/validation is incomplete.",
                f"MT5 broker symbols discovered: {pipeline.get('broker_symbols_returned', 0)}",
                f"Synthetic targets: {pipeline.get('synthetic_targets_detected', 0)} | Gold targets: {pipeline.get('gold_targets_detected', 0)}",
                f"Broker-verified targets: {pipeline.get('broker_verified_targets', 0)} | Enabled targets: {pipeline.get('enabled_targets', audit.get('target_count', 0))}",
                f"Usable: {audit.get('usable_count', 0)} | Invalid: {audit.get('invalid_count', 0)}",
                f"Reason: {capital.get('reason')}",
                "Action: use /brokercheck to inspect the complete symbol pipeline.",
            ])
        elif state == AccountCapitalState.ACCOUNT_STATE_UNKNOWN:
            audit = capital.get("broker_metadata") or {}
            text = "\n".join([
                "⚠️ ACCOUNT STATE UNKNOWN",
                "New trading is halted until MT5 account state is verified.",
                f"Reason: {capital.get('reason')}",
                f"Target symbols: {audit.get('target_count', 0)} | Usable: {audit.get('usable_count', 0)} | Invalid: {audit.get('invalid_count', 0)}",
                f"Top failure: {audit.get('top_failure', 'Broker account data unavailable')}",
                "Action: use /brokercheck for the field-by-field MT5 audit.",
            ])
        else:
            text = f"💰 DEMO CAPITAL STATUS: {state}\nReason: {capital.get('reason')}\nMinimum operating capital: {currency} {minimum:,.2f}"
        try:
            await self.bot_app.bot.send_message(chat_id=self.admin_chat_id, text=text)
        except Exception as exc:
            logger.error("Capital-state notification failed: %s", exc)

    @staticmethod
    def _component_label(component: dict, *, enabled_waiting: bool = False) -> str:
        state = str(component.get("state") or "NOT_STARTED")
        if state == "FAILED":
            return "🔴 FAILED"
        if state == "NOT_STARTED":
            return "🟡 WAITING" if enabled_waiting else "🟡 NOT STARTED"
        if state == "WAITING":
            return "🟡 WAITING"
        return "🟢 RUNNING"

    async def send_activity_heartbeat(self) -> None:
        """Send a ten-minute factual report, resetting only confirmed delivered-window counters."""
        self.telemetry.component_started("heartbeat")
        if not self.bot_app or not self.admin_chat_id:
            self.telemetry.component_succeeded("heartbeat", waiting=True)
            return
        try:
            capital = self.last_capital_state
            if not capital:
                capital = (await self.reconcile_account_state()).get("capital", {})
            account = capital.get("account") or {}
            audit = capital.get("broker_metadata") or self.capital_state_service.last_metadata_audit or {}
            state = str(capital.get("state") or AccountCapitalState.ACCOUNT_STATE_UNKNOWN)
            runtime = self.telemetry.snapshot(include_lifetime=True)
            window = runtime.get("window") or {}
            counters = window.get("counters") or {}
            components = runtime.get("components") or {}
            rejections = sorted((window.get("rejections") or {}).items(), key=lambda item: (-item[1], item[0]))[:3]
            errors = sorted((window.get("errors") or {}).items(), key=lambda item: (-item[1], item[0]))[:3]
            timeframe_text = ", ".join(f"{name}: {count}" for name, count in sorted((window.get("timeframes") or {}).items())) or "None"
            scanner = components.get("market_scanner", {})
            analysis = components.get("analysis_engine", {})
            execution = components.get("execution_engine", {})
            positions = components.get("position_manager", {})
            learning = components.get("learning_engine", {})
            heartbeat = components.get("heartbeat", {})
            trading = "🟢 ENABLED" if state not in AccountCapitalState.BLOCKING and not self.settings.is_paused and self.settings.auto_trade else "🔴 HALTED"
            scanner_label = self._component_label(scanner)
            analysis_label = self._component_label(analysis)
            execution_label = self._component_label(execution, enabled_waiting=bool(self.settings.auto_trade and not self.settings.is_paused))
            position_label = self._component_label(positions, enabled_waiting=True)
            learning_label = self._component_label(learning, enabled_waiting=True)
            overall = "🟢 ACTIVE" if counters.get("scan_cycles_completed", 0) > 0 and scanner.get("state") != "FAILED" else ("🔴 FAILED" if scanner.get("state") == "FAILED" else "🟡 AWAITING FIRST SCAN")
            position_count = self.last_account_reconciliation.get("broker_open_positions", 0)
            lines = [
                "🧠 BOT ACTIVITY — LAST 10 MINUTES",
                "", "SYSTEM",
                f"Heartbeat: {self._component_label(heartbeat)} | MT5: {'🟢 CONNECTED' if capital.get('current') else '🔴 UNAVAILABLE'} | Account: {str(account.get('broker_account_mode') or self.settings.trading_mode).upper()}",
                "", "MARKET ENGINE",
                f"Scanner: {scanner_label} | Scan cycles: {counters.get('scan_cycles_completed', 0)} complete / {counters.get('scan_cycles_failed', 0)} failed / {counters.get('scan_cycles_skipped_overlap', 0)} overlap-skipped",
                f"Last scan: {scanner.get('last_success') or 'never'} | Symbols attempted: {counters.get('symbols_attempted', 0)} | Analyzed: {counters.get('symbols_analyzed', 0)}",
                f"Candle requests: {counters.get('candle_requests', 0)} | Success: {counters.get('successful_candle_requests', 0)} | Failures: {counters.get('failed_candle_requests', 0)}",
                f"Timeframes actually requested: {timeframe_text}",
                "", "ANALYSIS",
                f"Analysis engine: {analysis_label} | Runs: {counters.get('analysis_runs', 0)} | Failures: {counters.get('analysis_failures', 0)}",
                f"Setups detected: {counters.get('setups_detected', 0)} | Setups rejected: {counters.get('setups_rejected', 0)}",
                "Top rejection reasons:",
                *([f"- {count}× {reason}" for reason, count in rejections] or ["- None recorded"]),
                "", "EXECUTION",
                f"Execution engine: {execution_label} | Trade candidates: {counters.get('trade_candidates', 0)}",
                f"Orders submitted: {counters.get('orders_submitted', 0)} | Filled: {counters.get('orders_filled', 0)} | Rejected: {counters.get('orders_rejected', 0)}",
                "", "POSITION MANAGEMENT",
                f"Position manager: {position_label} | Checked: {counters.get('positions_checked', 0)} | SL/TP modifications: {counters.get('positions_modified', 0)} | Closed: {counters.get('positions_closed', 0)}",
                "", "LEARNING",
                f"Learning engine: {learning_label} | Observations: {counters.get('observations', 0)} | Experiments: {counters.get('experiments', 0)} | Optimization runs: {counters.get('optimization_runs', 0)}",
                "", "ACCOUNT",
                f"Balance: {account.get('currency') or 'USD'} {float(account.get('balance') or 0.0):,.2f} | Equity: {account.get('currency') or 'USD'} {float(account.get('equity') or 0.0):,.2f}",
                f"Free margin: {account.get('currency') or 'USD'} {float(account.get('free_margin') or 0.0):,.2f} | Open positions: {position_count}",
                "", "UNIVERSE",
                f"Broker symbols: {(audit.get('pipeline') or {}).get('broker_symbols_returned', 0)} | Targets: {audit.get('target_count', 0)} | Usable: {audit.get('usable_count', 0)} | Invalid: {audit.get('invalid_count', 0)}",
                f"Capital state: {state} | Trading: {trading}",
                "", f"OVERALL STATUS: {overall}",
            ]
            if errors:
                lines.extend(["", "Runtime errors:", *[f"- {count}× {message}" for message, count in errors]])
            await self.bot_app.bot.send_message(chat_id=self.admin_chat_id, text="\n".join(lines))
        except Exception as exc:
            self.telemetry.component_failed("heartbeat", exc)
            logger.exception("Activity heartbeat notification failed")
            return
        self.telemetry.component_succeeded("heartbeat", waiting=True)
        self.telemetry.heartbeat_snapshot_and_reset()

    async def run_capital_reduction(self) -> dict:
        """Advance isolated capital management and record its actual runtime state."""
        self.telemetry.component_started("capital_management")
        try:
            result = await self._run_capital_reduction()
        except Exception as exc:
            self.telemetry.component_failed("capital_management", exc)
            raise
        self.telemetry.component_succeeded("capital_management", waiting=result.get("state") in {"idle", "paused", "completed"})
        return result

    async def _run_capital_reduction(self) -> dict:
        """Advance the isolated DEMO reduction engine; it never feeds the optimizer."""
        self.capital_reduction.settings = self.settings
        self.capital_reduction.executor = self.executor
        result = await self.capital_reduction.run_once()
        if result.get("state") in {"completed", "blocked", "failed", "paused"}:
            session_id = result.get("session_id", "?")
            await self._chart_activity(
                "capital_reduction_state", "SYSTEM",
                f"🔥 **CAPITAL REDUCTION UPDATE**\nSession: `#{session_id}` | State: `{result.get('state', 'unknown').upper()}`\n{result.get('reason', '')}",
                fingerprint=f"capital:{session_id}:{result.get('state')}:{result.get('reason', '')}",
                essential=True,
            )
        return result

    async def activate_and_scan_now(self) -> bool:
        """Refresh the broker universe and begin the first scan immediately after activation.

        This is deliberately asynchronous so the Telegram confirmation returns
        promptly. It preserves all normal eligibility, duplicate, broker-validity,
        and fail-closed checks inside ``scan_and_execute``.
        """
        logger.info("Autonomous execution activated; starting immediate broker-universe refresh and scan")
        ready = await self.refresh_market_universe()
        if not ready:
            logger.warning("Immediate activation scan skipped because no verified broker instrument is available")
            return False
        await self.scan_and_execute()
        return True

    async def stop(self):
        """Stop the scanner."""
        self.scheduler.shutdown(wait=False)
        self._running = False
        await self.data_provider.close()

    async def refresh_market_universe(self) -> bool:
        """Discover broker targets and record the true universe-component outcome."""
        self.telemetry.component_started("market_universe")
        try:
            result = await self._refresh_market_universe()
        except Exception as exc:
            self.telemetry.component_failed("market_universe", exc)
            raise
        self.telemetry.component_succeeded("market_universe", waiting=not result)
        return result

    async def _refresh_market_universe(self) -> bool:
        """Discover and persist the connected account's eligible Deriv markets.

        Discovery is intentionally authoritative: legacy configured forex symbols
        are discarded. A failed discovery clears the active set, so a broken
        broker connection cannot lead to a stale or guessed execution symbol.
        """
        try:
            records = await self.market_universe.refresh(self.executor)
        except Exception as exc:
            logger.error("Deriv market-universe discovery failed: %s", exc)
            self.market_universe.last_refresh_error = f"Unhandled refresh error: {type(exc).__name__}: {exc}"
            records = []

        try:
            json_path, markdown_path = self.market_universe.write_audit_report("logs")
            self.last_universe_audit_paths = (str(json_path), str(markdown_path))
            logger.info("MT5 symbol discovery audit written: %s", markdown_path)
        except Exception as exc:
            logger.exception("Could not write MT5 symbol discovery audit: %s", exc)
            self.last_universe_audit_paths = None

        active = self.market_universe.available_symbols
        synthetic_targets = [record.symbol for record in records if record.category == "synthetic_index"]
        gold_targets = [record.symbol for record in records if record.category == "gold"]
        pipeline = {
            "broker_symbols_returned": len(records),
            "synthetic_targets_detected": len(synthetic_targets),
            "gold_targets_detected": len(gold_targets),
            "broker_verified_targets": len(active),
            "broker_verified_target_symbols": list(active),
            "universe_refresh_error": self.market_universe.last_refresh_error or None,
            "stage": "broker_classification_complete",
        }
        logger.info("[UNIVERSE] MT5 returned %s symbols", len(records))
        logger.info("[CLASSIFIER] Synthetic targets: %s | Gold targets: %s", len(synthetic_targets), len(gold_targets))
        logger.info("[BROKER VERIFY] Verified targets: %s | %s", len(active), active)
        # Discovery is authoritative and fail-closed. It deliberately clears a
        # prior universe even when MT5 returned records that were all rejected.
        self.settings.symbols = [record.symbol for record in self.market_universe.accepted_records]
        self.settings.available_symbols = list(active)
        self.settings.enabled_symbols = list(active)
        self.settings.unsupported_symbols = self.market_universe.unsupported_symbols
        self.settings.symbol_status = {
            record.symbol: f"{record.status}: {record.decision_reason}" for record in records
        }
        self.settings.market_universe_updated_at = datetime.utcnow().isoformat()
        await db.save_settings(self.settings)
        # Give the validator the completed broker universe directly instead of
        # making it depend on settings persistence or subsequent reload timing.
        pipeline.update({"stage": "enabled_targets_populated", "enabled_targets": len(self.settings.enabled_symbols), "enabled_target_symbols": list(self.settings.enabled_symbols)})
        self.capital_state_service.set_verified_target_universe(active, pipeline)
        logger.info("[ENABLED] Enabled targets: %s | %s", len(self.settings.enabled_symbols), self.settings.enabled_symbols)

        eligible_count = sum(1 for record in records if record.category in {"synthetic_index", "gold"})
        logger.info(
            "Deriv market universe audit: %s returned / %s accepted / %s rejected / %s scope-matched",
            len(records), len(active), len(self.market_universe.rejected_records), eligible_count,
        )
        if not active:
            reason = self.market_universe.last_refresh_error or "No broker-returned symbol met the allowed Deriv Synthetic Index / Gold and trade-mode criteria"
            await self._chart_activity(
                "broker_unavailable", "SYSTEM",
                "⚠️ **BROKER UNIVERSE FAIL-CLOSED**\n"
                f"Active instruments: `0`\nReason: {reason}\n"
                "The full accepted/rejected MT5 metadata audit was written to the VPS logs directory. No guessed symbol was enabled.",
                fingerprint=f"no-eligible-deriv-markets:{reason}", essential=True,
            )
            return False
        return True

    async def _execution_symbol_spec(self, symbol: str, direction: str) -> dict:
        """Return broker-native volume, tick, contract, and minimum-lot margin evidence."""
        spec = dict(await self.executor.get_symbol_info(symbol) or {})
        metadata = await self.executor.get_symbol_execution_metadata(symbol, direction)
        if not metadata:
            return spec
        minimum_volume = metadata.get("normalized_volume") or metadata.get("volume_min")
        spec.update({
            "volume_min": metadata.get("volume_min", spec.get("min_lot")),
            "volume_max": metadata.get("volume_max", spec.get("max_lot")),
            "volume_step": metadata.get("volume_step", spec.get("step_lot")),
            "normalized_volume": minimum_volume,
            "margin_required_min_volume": metadata.get("margin_required"),
            "margin_initial": metadata.get("margin_initial"),
            "trade_tick_size": metadata.get("tick_size", spec.get("tick_size")),
            "trade_tick_value": metadata.get("tick_value", spec.get("tick_value")),
            "trade_contract_size": metadata.get("trade_contract_size", spec.get("contract_size")),
        })
        return spec

    async def fetch_candles(self, symbol: str, timeframe: str, count: int = 200) -> "pd.DataFrame":
        """Fetch broker-native, closed OHLCV data and record the actual outcome."""
        self.telemetry.increment("candle_requests")
        self.telemetry.record_timeframe(timeframe)
        try:
            frame = await self.data_provider.get_candles(symbol, timeframe, count)
        except Exception as exc:
            self.telemetry.increment("failed_candle_requests")
            self.telemetry.record_error(f"candle {symbol} {timeframe}: {type(exc).__name__}: {exc}")
            logger.exception("[CANDLE FAILURE] %s %s", symbol, timeframe)
            raise
        if frame is None or frame.empty:
            self.telemetry.increment("failed_candle_requests")
            self.telemetry.record_error(f"candle {symbol} {timeframe}: empty broker response")
            logger.warning("[CANDLE FAILURE] %s %s returned no closed candles", symbol, timeframe)
        else:
            self.telemetry.increment("successful_candle_requests")
        return frame

    @staticmethod
    def _validation_snapshot(validation) -> dict:
        """Create a compact, JSON-safe account of causal setup validity gates."""
        sweep = getattr(validation, "sweep", None)
        zone = getattr(validation, "zone", None)
        displacement = getattr(validation, "displacement", None)
        target = getattr(validation, "target_pool", None)
        return {
            "valid": bool(getattr(validation, "valid", False)),
            "entry_mode": getattr(getattr(validation, "entry_mode", None), "value", ""),
            "checks": [
                {"name": check.name, "passed": bool(check.passed), "detail": check.detail}
                for check in getattr(validation, "checks", [])
            ],
            "zone": ({"source": zone.source, "top": zone.top, "bottom": zone.bottom, "detail": zone.detail} if zone else None),
            "sweep": ({"pool_level": sweep.pool.level, "pool_side": sweep.pool.side.value, "index": sweep.index} if sweep else None),
            "displacement": ({"confirmed": bool(displacement.confirmed), "index": displacement.index, "detail": displacement.detail} if displacement else None),
            "target": ({"level": target.level, "side": target.side.value, "kind": target.kind.value} if target else None),
        }

    @staticmethod
    def _feature_snapshot(df, structure, htf_structures, atr_value: float, validation=None) -> dict:
        """Capture observable closed-candle state and testable SMC feature flags."""
        event = structure.last_event
        checks = {check.name: bool(check.passed) for check in getattr(validation, "checks", [])}
        return {
            "bar_time": str(df.iloc[-1]["time"]),
            "close": float(df.iloc[-1]["close"]),
            "atr": float(atr_value),
            "ltf_trend": structure.trend.value,
            "ltf_zone": structure.current_zone,
            "structure_event": event.event_type.value,
            "structure_event_index": int(event.index),
            "htf_trends": [item.trend.value for item in htf_structures],
            "htf_events": [item.last_event.event_type.value for item in htf_structures],
            "htf_context": checks.get("HTF context", False),
            "meaningful_liquidity": checks.get("Meaningful liquidity", False),
            "liquidity_sweep": checks.get("Liquidity sweep", False),
            "directional_displacement": checks.get("Directional displacement", False),
            "bos_choch": checks.get("BOS/CHOCH confirmation", False),
            "zone_retest": checks.get("Retracement into valid zone", False),
            "zone_order_block": getattr(getattr(validation, "zone", None), "source", "") == "order_block",
            "zone_fvg": getattr(getattr(validation, "zone", None), "source", "") == "fvg",
            "zone_supply_demand": getattr(getattr(validation, "zone", None), "source", "") == "supply_demand",
            "ltf_confirmation": checks.get("LTF confirmation", False),
        }

    async def _evaluate_counterfactuals(self, symbol: str, timeframe: str, df) -> None:
        """Resolve rejected setups only with candles that closed after detection.

        If a single candle can hit both hypothetical stop and target, bar-level
        data cannot establish the event order. The outcome remains explicitly
        ambiguous instead of assuming a profitable path.
        """
        pending = await db.get_pending_counterfactual_setups(
            self.settings.trading_mode, symbol, timeframe
        )
        for setup in pending:
            features = setup.get("features") or {}
            detected_at = features.get("bar_time")
            if not detected_at:
                continue
            later = df[df["time"] > pd.to_datetime(detected_at, utc=True)]
            if later.empty:
                continue
            entry = float(setup["entry_price"])
            stop = float(setup["stop_loss"])
            target = float(setup["take_profit"])
            risk = abs(entry - stop)
            if risk <= 0:
                continue
            direction = str(setup["direction"]).upper()
            mfe, mae = 0.0, 0.0
            resolved = None
            for _, bar in later.iterrows():
                high, low = float(bar["high"]), float(bar["low"])
                favorable = ((high - entry) if direction == "BUY" else (entry - low)) / risk
                adverse = ((low - entry) if direction == "BUY" else (entry - high)) / risk
                mfe, mae = max(mfe, favorable), min(mae, adverse)
                stop_hit = low <= stop if direction == "BUY" else high >= stop
                target_hit = high >= target if direction == "BUY" else low <= target
                if stop_hit and target_hit:
                    resolved = "counterfactual_ambiguous"
                    break
                if target_hit:
                    resolved = "counterfactual_win"
                    break
                if stop_hit:
                    resolved = "counterfactual_loss"
                    break
            if resolved:
                await db.update_setup_record(
                    int(setup["id"]),
                    status=resolved,
                    outcome={
                        "result": resolved,
                        "mfe_r": mfe,
                        "mae_r": mae,
                        "resolved_on": str(later.iloc[-1]["time"]),
                    },
                )

    async def analyze_symbol(self, symbol: str, *, record_learning: bool = True) -> Optional[TradeSignal]:
        """
        Full analysis of a single symbol across all timeframes.
        Returns a TradeSignal if a tradeable setup is found, else None.
        """
        await self._reload_settings()
        self.telemetry.increment("analysis_runs")
        self.telemetry.increment("symbols_analyzed")
        self.telemetry.increment("observations")
        self.optimizer.settings = self.settings
        policy, experiment_id, policy_version = await self.optimizer.active_policy(self.settings.trading_mode)
        if not self._analysis_symbol_is_eligible(symbol):
            reason = "Symbol is absent from the current broker-validated usable-target handoff"
            self.telemetry.increment("setups_rejected")
            self.telemetry.record_rejection(reason)
            logger.warning("[ANALYSIS PRE-CANDLE REJECTED] symbol=%s broker_usable=%s settings_enabled=%s", symbol, len(self._analysis_eligible_symbols), len(self.settings.enabled_symbols))
            return None

        # Fetch data for primary timeframe
        primary_tf = self.settings.timeframes[0] if self.settings.timeframes else "M15"
        df = await self.fetch_candles(symbol, primary_tf, 200)

        if df.empty or len(df) < 20:
            reason = "Insufficient closed broker candles for structural analysis"
            self.telemetry.increment("setups_rejected")
            self.telemetry.record_rejection(reason)
            logger.warning(f"Insufficient data for {symbol}")
            await self._chart_activity(
                "data_unavailable", symbol,
                f"⚠️ **CHART STUDY PAUSED — {symbol}**\nTimeframe: `{primary_tf}`\nReason: insufficient closed broker candles for structural analysis.",
                fingerprint=f"{primary_tf}:insufficient:{len(df)}",
            )
            return None
        bar_time = str(df.iloc[-1]["time"])
        current_price = float(df.iloc[-1]["close"])
        await self._chart_activity(
            "study_started", symbol,
            f"🔎 **CHART STUDY STARTED — {symbol}**\nClosed candle: `{bar_time}` ({primary_tf})\nClose: `{current_price:.5f}`\nNext: mapping structure, liquidity, displacement, and valid zones.",
            fingerprint=f"{primary_tf}:{bar_time}",
        )
        await self._evaluate_counterfactuals(symbol, primary_tf, df)

        # Run structure analysis
        structure = analyze_structure(df, lookback=3)
        
        # Run S/D zone detection
        zones = detect_sd_zones(df, lookback=100)

        # 2.5 Adaptive Profiling
        profile = await profiler.profile_symbol(
            symbol,
            df,
            timeframe=primary_tf,
            account_mode=self.settings.trading_mode,
        )
        
        

        # Fetch HTF structures for confluence
        htf_structures = []
        for htf in self.settings.htf_timeframes[:2]:
            htf_df = await self.fetch_candles(symbol, htf, 200)
            if not htf_df.empty and len(htf_df) >= 20:
                htf_struct = analyze_structure(htf_df, lookback=3)
                htf_structures.append(htf_struct)

        # Determine trade direction from the current closed-candle structure.
        event_name = structure.last_event.event_type.value.replace("_", " ").upper()
        await self._chart_activity(
            "structure_mapped", symbol,
            f"🧭 **STRUCTURE MAPPED — {symbol}**\nTrend: `{structure.trend.value.upper()}` | Zone: `{structure.current_zone.upper()}`\nLatest structural event: `{event_name}`\nHTF alignment observed: `{len(htf_structures)}/{min(2, len(self.settings.htf_timeframes))}` timeframe(s).",
            fingerprint=f"{bar_time}:{structure.trend.value}:{structure.current_zone}:{structure.last_event.event_type.value}",
        )

        # Set paper prices so PaperExecutor can execute (if still in use by backtester)
        if hasattr(self.executor, 'set_price'):
            spread_est = current_price * 0.0002  # ~2 pip spread estimate
            self.executor.set_price(symbol, current_price - spread_est, current_price + spread_est)

        if structure.trend == Trend.BULLISH:
            direction = "BUY"
        elif structure.trend == Trend.BEARISH:
            direction = "SELL"
        else:
            # Ranging — look for zone-based reversal
            if structure.current_zone == "discount":
                direction = "BUY"
            elif structure.current_zone == "premium":
                direction = "SELL"
            else:
                self.telemetry.increment("setups_rejected")
                self.telemetry.record_rejection("No directional structure or valid premium/discount reversal context")
                return None  # No clear direction

        # The active experimental policy chooses how the observed market features
        # are used.  No global SMC sequence is universally required in DEMO.
        try:
            entry_mode = EntryMode.CONFIRMED if policy.entry_model == "confirmation" else EntryMode.AGGRESSIVE
        except (AttributeError, ValueError):
            entry_mode = EntryMode.AGGRESSIVE

        # Confirmed models use a lower timeframe where available. M1 is already
        # the lowest supported timeframe, so it validates on its own closed bars.
        ltf_df = df
        if primary_tf not in ("M1", "M5"):
            candidate_ltf = await self.fetch_candles(symbol, "M5", 200)
            if not candidate_ltf.empty:
                ltf_df = candidate_ltf

        atr_val = atr(df, 14).iloc[-1]
        if atr_val <= 0 or (isinstance(atr_val, float) and atr_val != atr_val):
            atr_val = current_price * 0.002

        validator = SetupValidator(
            min_rr=0.0,
            min_sweep_penetration_atr=self.settings.liquidity_sweep_min_penetration_atr,
            displacement_body_ratio=self.settings.displacement_body_ratio_min,
            displacement_range_ratio=self.settings.displacement_range_ratio_min,
            stop_atr_buffer=policy.stop_atr_buffer if policy.stop_atr_buffer is not None else self.settings.structural_stop_atr_buffer,
            require_ltf_confirmation=False,
        )
        validation = validator.observe(
            symbol=symbol,
            direction=direction,
            timeframe=primary_tf,
            df=df,
            structure=structure,
            htf_structures=htf_structures,
            zones=zones,
            entry_mode=entry_mode,
            ltf_df=ltf_df,
            target_rr=policy.rr_target,
            stop_model=policy.stop_model,
            target_model=policy.target_model,
        )
        setup_id = None
        if record_learning:
            setup_id = await db.record_setup(
                account_mode=self.settings.trading_mode,
                symbol=symbol,
                timeframe=primary_tf,
                direction=direction,
                setup_type="Liquidity Sweep Reversal",
                status="candidate" if validation.valid else "rejected",
                rejection_reason=validation.rejection_reason,
                entry_price=validation.entry_price or current_price,
                stop_loss=validation.stop_loss or None,
                take_profit=validation.take_profit or None,
                rr_ratio=validation.rr_ratio or None,
                validation=self._validation_snapshot(validation),
                features=self._feature_snapshot(df, structure, htf_structures, float(atr_val), validation),
                policy_version=policy_version or self.settings.active_model_version,
                experiment_id=experiment_id,
            )
        if not validation.valid:
            # Malformed price/stop/target data is an operational integrity fault,
            # not a failed SMC hypothesis. It must never reach the broker.
            reason = "Candidate lacks broker-executable stop/target geometry"
            self.telemetry.increment("setups_rejected")
            self.telemetry.record_rejection(reason)
            if setup_id is not None:
                await db.update_setup_record(setup_id, status="invalidated", rejection_reason=reason)
            await self._chart_activity(
                "validation_rejected", symbol,
                f"⛔ **CANDIDATE INVALID — {symbol}**\nReason: {reason}\nNo order will be considered from this closed candle.",
                fingerprint=f"{bar_time}:{direction}:invalid_geometry",
            )
            return None

        # Scoring remains descriptive telemetry. The active policy determines
        # eligibility instead of a global score, RR, or SMC gate.
        quality = score_setup_quality(
            validation, structure, min_score=0.0,
            extreme_score=self.settings.extreme_setup_score, historical_expectancy_r=None,
        )
        features = self._feature_snapshot(df, structure, htf_structures, float(atr_val), validation)
        policy_ok, policy_reason = policy.accepts(
            score=quality.score, rr_ratio=validation.rr_ratio, features=features,
        )
        if not policy_ok:
            self.telemetry.increment("setups_rejected")
            self.telemetry.record_rejection(policy_reason)
            if setup_id is not None:
                await db.update_setup_record(setup_id, status="rejected", rejection_reason=policy_reason)
            await self._chart_activity(
                "policy_rejected", symbol,
                f"🧪 **POLICY SAMPLE DEFERRED — {symbol}**\nThe candidate remains stored for counterfactual analysis.\nActive policy: `{policy_version or self.settings.active_model_version}`\nReason: {policy_reason}",
                fingerprint=f"{bar_time}:{direction}:{policy.fingerprint}:{policy_reason}",
            )
            return None

        await self._chart_activity(
            "setup_validated", symbol,
            f"✅ **EXPERIMENT CANDIDATE ACCEPTED — {symbol}**\nPolicy: `{policy_version or self.settings.active_model_version}` | Direction: `{direction}` | Timeframe: `{primary_tf}`\nFeature rank: `{quality.score:.1f}/100` | Entry: `{validation.entry_price:.5f}` | SL: `{validation.stop_loss:.5f}` | TP: `{validation.take_profit:.5f}` | RR: `1:{validation.rr_ratio:.2f}`",
            fingerprint=f"{bar_time}:{direction}:{policy.fingerprint}:{validation.entry_price}:{validation.stop_loss}:{validation.take_profit}",
        )

        signal = TradeSignal(
            symbol=symbol,
            direction=direction,
            entry_price=validation.entry_price,
            stop_loss=validation.stop_loss,
            take_profit=validation.take_profit,
            score=quality.score,
            rr_ratio=validation.rr_ratio,
            suggested_risk=float(policy.risk_pct or self.settings.risk_per_trade),
            structure=structure,
            zones=zones,
            timeframe=primary_tf,
            entry_mode=policy.entry_model,
            setup_type="Experimental Market Candidate",
            created_at=datetime.utcnow().isoformat(),
            expires_at=(datetime.utcnow() + timedelta(minutes=self.settings.max_signal_age_minutes)).isoformat(),
            validation=validation,
            quality_factors=quality.factors,
            target_source=validation.target_pool.kind.value if validation.target_pool else "policy_rr_fallback",
            setup_id=setup_id,
            passed=True,
            rejection_reason="",
        )
        signal.policy_version = policy_version or self.settings.active_model_version
        signal.experiment_id = experiment_id
        signal.experimental_policy = policy.to_dict()
        return signal

    async def scan_markets(self) -> list[TradeSignal]:
        """Scan only the current broker-validated usable handoff and return accepted signals."""
        signals = []
        for symbol in self._analysis_eligible_symbols:
            try:
                signal = await self.analyze_symbol(symbol)
                if signal and signal.passed:
                    signals.append(signal)
            except Exception as e:
                logger.error(f"Error analyzing {symbol}: {e}")
        return signals

    async def execute_signal(self, signal: TradeSignal, df: pd.DataFrame = None) -> bool:
        """Run actual execution with factual component health instrumentation."""
        self.telemetry.component_started("execution_engine")
        try:
            result = await self._execute_signal(signal, df)
        except Exception as exc:
            self.telemetry.component_failed("execution_engine", exc)
            raise
        self.telemetry.component_succeeded("execution_engine", waiting=True)
        return result

    async def _execute_signal(self, signal: TradeSignal, df: pd.DataFrame = None) -> bool:
        """Run risk checks and submit a broker order only if the candidate remains valid."""
        symbol = signal.symbol
        try:
            # Final revalidation immediately before any market order. A signal
            # approval or prior scan never freezes market structure or pricing.
            setup_id = signal.setup_id
            if signal.expires_at and datetime.utcnow() > datetime.fromisoformat(signal.expires_at):
                signal.passed = False
                signal.rejection_reason = "Signal expired before execution"
                self.telemetry.record_rejection(signal.rejection_reason)
                if setup_id is not None:
                    await db.update_setup_record(setup_id, status="expired", rejection_reason=signal.rejection_reason)
                await self._chart_activity(
                    "execution_rejected", symbol,
                    f"⌛ **EXECUTION CANCELLED — {symbol}**\nReason: {signal.rejection_reason}",
                    fingerprint=f"{setup_id}:expired", essential=True,
                )
                return False
            refreshed = await self.analyze_symbol(symbol, record_learning=False)
            if not refreshed or not refreshed.passed or refreshed.direction != signal.direction:
                signal.passed = False
                signal.rejection_reason = "Setup invalidated during final revalidation"
                self.telemetry.record_rejection(signal.rejection_reason)
                if setup_id is not None:
                    await db.update_setup_record(setup_id, status="invalidated", rejection_reason=signal.rejection_reason)
                await self._chart_activity(
                    "execution_rejected", symbol,
                    f"⛔ **FINAL REVALIDATION FAILED — {symbol}**\nReason: {signal.rejection_reason}\nNo market order was submitted.",
                    fingerprint=f"{setup_id}:invalidated", essential=True,
                )
                return False
            signal = refreshed
            signal.setup_id = setup_id
            await self._chart_activity(
                "final_revalidation", symbol,
                f"🔬 **FINAL REVALIDATION PASSED — {symbol}**\nDirection: `{signal.direction}` | Entry: `{signal.entry_price:.5f}` | RR: `1:{signal.rr_ratio:.2f}`\nNext: sizing, portfolio exposure, margin, and daily-risk review.",
                fingerprint=f"{setup_id}:{signal.entry_price}:{signal.stop_loss}:{signal.take_profit}",
            )

            account = await self.executor.get_account_info()
            equity = float(account.get("equity", account.get("balance", 0)))
            free_margin = float(account.get("free_margin", 0))
            leverage = float(account.get("leverage", 1) or 1)
            today_pnl = await db.get_today_pnl(self.settings.trading_mode)
            today_count = await db.get_today_trade_count(self.settings.trading_mode)
            consecutive_losses = await db.get_consecutive_losses(account_mode=self.settings.trading_mode)
            open_positions = await self.executor.get_open_positions()
            sym_info = await self._execution_symbol_spec(symbol, signal.direction)
            pip = sym_info.get("pip_size", pip_value(symbol))
            spread = sym_info.get("spread", 0) * pip

            # Calculate present account exposure at each position's protective SL.
            current_open_risk = 0.0
            for position in open_positions:
                position_info = await self.executor.get_symbol_info(position.symbol)
                position_risk = self.risk_manager.calculate_position_risk(position, position_info)
                if position_risk == float("inf"):
                    signal.passed = False
                    signal.rejection_reason = f"Unprotected open position: {position.symbol} #{position.ticket}"
                    self.telemetry.record_rejection(signal.rejection_reason)
                    await self._chart_activity(
                        "execution_rejected", symbol,
                        f"⛔ **SAFETY BLOCK — {symbol}**\nReason: {signal.rejection_reason}\nA new trade will not be opened while an existing position lacks structural protection.",
                        fingerprint=f"{setup_id}:unprotected:{position.ticket}", essential=True,
                    )
                    return False
                current_open_risk += position_risk

            sizing = self.risk_manager.calculate_position_sizing(
                account_equity=equity,
                free_margin=free_margin,
                entry_price=signal.entry_price,
                stop_loss=signal.stop_loss,
                symbol_info=sym_info,
                leverage=leverage,
                risk_pct=float(signal.experimental_policy.get("risk_pct", signal.suggested_risk)),
                risk_model=str(signal.experimental_policy.get("risk_model", "fixed_pct")),
                fixed_volume=signal.experimental_policy.get("fixed_volume"),
            )
            if not sizing.valid:
                signal.passed = False
                signal.rejection_reason = f"Sizing rejected: {sizing.reason}"
                self.telemetry.record_rejection(signal.rejection_reason)
                if signal.setup_id is not None:
                    await db.update_setup_record(signal.setup_id, status="sizing_rejected", rejection_reason=signal.rejection_reason)
                    await db.record_execution_event(
                        account_mode=self.settings.trading_mode,
                        symbol=symbol,
                        setup_id=signal.setup_id,
                        status="sizing_rejected",
                        requested_price=signal.entry_price,
                        reason=signal.rejection_reason,
                        details={"free_margin": free_margin, "sizing": sizing.evidence()},
                    )
                await self._chart_activity(
                    "execution_rejected", symbol,
                    f"⛔ **SIZING REJECTED — {symbol}**\n"
                    f"Code: `{sizing.sizing_code or 'UNSPECIFIED'}`\nReason: {signal.rejection_reason}\n"
                    f"Policy-required lot: `{sizing.policy_required_lot:.8g}` | Broker-normalized required lot: `{sizing.required_lot:.8g}`\n"
                    f"Broker min / step: `{sizing.broker_min_lot:g}` / `{sizing.broker_volume_step:g}`\n"
                    f"Min-lot margin: `${sizing.minimum_lot_margin:.2f}` | Min-lot loss: `${sizing.minimum_lot_loss:.2f}`\n"
                    f"Free margin: `${sizing.available_margin:.2f}` | Required margin: `${sizing.required_margin:.2f}`\nNo order was submitted.",
                    fingerprint=f"{setup_id}:sizing:{signal.rejection_reason}",
                )
                return False

            # Reserve risk for the planned basket now. Layers are not blindly
            # opened together: only L1 executes; each later layer is contingent
            # on fresh thesis confirmation and remaining basket risk.
            layers = self.risk_manager.get_layering_plan(
                sizing.final_volume,
                signal.entry_price,
                signal.stop_loss,
                sym_info,
                account_equity=equity,
                allocation=list(signal.experimental_policy.get("layer_allocation") or [1.0]),
                max_layers=int(signal.experimental_policy.get("max_layers", 0)),
                layer_style=str(signal.experimental_policy.get("layer_style", "none")),
            )
            if not layers:
                signal.passed = False
                signal.rejection_reason = "No broker-valid initial layer"
                self.telemetry.record_rejection(signal.rejection_reason)
                if signal.setup_id is not None:
                    await db.update_setup_record(signal.setup_id, status="sizing_rejected", rejection_reason=signal.rejection_reason)
                    await db.record_execution_event(
                        account_mode=self.settings.trading_mode,
                        symbol=symbol,
                        setup_id=signal.setup_id,
                        status="sizing_rejected",
                        requested_price=signal.entry_price,
                        reason=signal.rejection_reason,
                    )
                await self._chart_activity(
                    "execution_rejected", symbol,
                    f"⛔ **BROKER VOLUME BLOCK — {symbol}**\nReason: {signal.rejection_reason}\nNo valid initial layer exists within the broker’s volume rules.",
                    fingerprint=f"{setup_id}:volume:{signal.rejection_reason}",
                )
                return False
            initial_layer = layers[0]

            risk_result = await self.risk_manager.check_all(
                symbol=symbol,
                direction=signal.direction,
                score=signal.score,
                rr_ratio=signal.rr_ratio,
                spread_pips=spread / pip if pip > 0 else 0,
                account_equity=equity,
                free_margin=free_margin,
                required_margin=sizing.required_margin,
                today_pnl=today_pnl,
                today_trade_count=today_count,
                open_position_count=len(open_positions),
                proposed_setup_risk=sizing.expected_loss,
                current_open_risk=current_open_risk,
                setup_valid=bool(signal.validation and signal.validation.valid),
                consecutive_losses=consecutive_losses,
                policy=signal.experimental_policy,
            )
            if not risk_result.passed:
                logger.info(f"Signal rejected for {symbol}: {risk_result.reason}")
                signal.passed = False
                signal.rejection_reason = risk_result.reason
                self.telemetry.record_rejection(signal.rejection_reason)
                if signal.setup_id is not None:
                    await db.update_setup_record(signal.setup_id, status="risk_rejected", rejection_reason=risk_result.reason)
                    await db.record_execution_event(
                        account_mode=self.settings.trading_mode,
                        symbol=symbol,
                        setup_id=signal.setup_id,
                        status="risk_rejected",
                        requested_price=signal.entry_price,
                        reason=risk_result.reason,
                    )
                await self._chart_activity(
                    "execution_rejected", symbol,
                    f"⛔ **PORTFOLIO RISK BLOCK — {symbol}**\nReason: {signal.rejection_reason}\nThe structural setup remains recorded, but no order was sent.",
                    fingerprint=f"{setup_id}:risk:{signal.rejection_reason}", essential=True,
                )
                return False

            await self._chart_activity(
                "broker_submission", symbol,
                f"📤 **BROKER ORDER SUBMITTED — {symbol}**\nDirection: `{signal.direction}` | Required volume: `{initial_layer['lot']}`\n"
                f"Policy-required lot: `{sizing.policy_required_lot:.8g}` | Broker-normalized lot: `{sizing.required_lot:.8g}`\n"
                f"Risk reserved: `${sizing.expected_loss:.2f}` | Required margin: `${sizing.required_margin:.2f}` | Free margin: `${sizing.available_margin:.2f}`\n"
                f"SL: `{signal.stop_loss:.5f}` | TP: `{signal.take_profit:.5f}`\nAwaiting broker response.",
                fingerprint=f"{setup_id}:submit:{initial_layer['lot']}:{signal.entry_price}", essential=True,
            )
            execution_started = perf_counter()
            self.telemetry.increment("orders_submitted")
            result = await self.executor.execute_trade(
                symbol=symbol,
                direction=signal.direction,
                lot_size=initial_layer["lot"],
                sl=signal.stop_loss,
                tp=signal.take_profit,
                magic=self.settings.magic_number,
                comment=initial_layer["comment"],
            )

            if result.success:
                self.telemetry.increment("orders_filled")
                raw_signal = {
                    "entry_mode": signal.entry_mode,
                    "setup_type": signal.setup_type,
                    "target_source": signal.target_source,
                    "initial_layer": initial_layer,
                    "planned_layers": layers,
                    "sizing": sizing.evidence(),
                    "policy_version": signal.policy_version,
                    "experiment_id": signal.experiment_id,
                    "experimental_policy": signal.experimental_policy,
                    "quality_factors": [
                        {"name": factor.name, "points": factor.points, "maximum": factor.maximum, "detail": factor.detail}
                        for factor in signal.quality_factors
                    ],
                    "legacy_factors": [{"name": factor.name, "score": factor.score, "detail": factor.detail} for factor in signal.factors],
                }
                trade_id = await db.record_trade(
                    symbol=symbol,
                    direction=signal.direction,
                    entry_price=result.entry_price,
                    sl_price=result.sl,
                    tp_price=result.tp,
                    lot_size=result.lot_size,
                    score=signal.score,
                    rr_ratio=signal.rr_ratio,
                    executor=self.executor.name,
                    raw_signal=json.dumps(raw_signal),
                    account_mode=self.settings.trading_mode,
                    ticket=result.ticket,
                    setup_id=signal.setup_id,
                    initial_risk=sizing.expected_loss,
                    policy_version=signal.policy_version,
                    experiment_id=signal.experiment_id,
                    demo_session_id=(self.last_capital_state.get("demo_session_id") if self.settings.trading_mode == "demo" else None),
                )
                if signal.setup_id is not None:
                    await db.update_setup_record(signal.setup_id, status="executed", trade_id=trade_id)
                await db.record_execution_event(
                    account_mode=self.settings.trading_mode,
                    symbol=symbol,
                    setup_id=signal.setup_id,
                    trade_id=trade_id,
                    ticket=result.ticket,
                    requested_price=signal.entry_price,
                    executed_price=result.entry_price,
                    execution_delay_ms=(perf_counter() - execution_started) * 1000,
                    status="filled",
                    details={"lot_size": result.lot_size, "entry_mode": signal.entry_mode},
                )
                basket_id = await db.create_trade_basket(
                    symbol=symbol,
                    direction=signal.direction,
                    entry_price=result.entry_price,
                    initial_stop=result.sl,
                    initial_target=result.tp,
                    max_risk=sizing.risk_amount,
                    reserved_risk=sizing.expected_loss,
                    planned_layers=layers,
                    metadata={
                        "trade_id": trade_id,
                        "entry_mode": signal.entry_mode,
                        "setup_type": signal.setup_type,
                        "quality_score": signal.score,
                        "rr_ratio": signal.rr_ratio,
                        "policy_version": signal.policy_version,
                        "experiment_id": signal.experiment_id,
                        "experimental_policy": signal.experimental_policy,
                    },
                    account_mode=self.settings.trading_mode,
                    policy_version=signal.policy_version,
                    experiment_id=signal.experiment_id,
                )
                await db.record_trade_layer(
                    basket_id=basket_id,
                    ticket=result.ticket,
                    layer_number=initial_layer["number"],
                    planned_volume=initial_layer["lot"],
                    executed_volume=result.lot_size,
                    entry_price=result.entry_price,
                    stop_loss=result.sl,
                    take_profit=result.tp,
                    status="open",
                    trigger_reason=initial_layer["trigger"],
                )
                for planned_layer in layers[1:]:
                    await db.record_trade_layer(
                        basket_id=basket_id,
                        layer_number=planned_layer["number"],
                        planned_volume=planned_layer["lot"],
                        status="planned",
                        trigger_reason=planned_layer["trigger"],
                    )
                if result.ticket is not None:
                    await db.log_basket_action(
                        basket_id=basket_id,
                        ticket=result.ticket,
                        action="Initial Layer Executed",
                        details={
                            "layer": initial_layer["number"],
                            "reserved_risk": sizing.expected_loss,
                            "remaining_reserved_risk": max(0.0, sizing.expected_loss - initial_layer["expected_loss"]),
                        },
                        trade_id=trade_id,
                    )
                await db.set_symbol_cooldown(symbol)
                signal.passed = True
                
                # Render chart for executed trade if df is provided
                photo = None
                if df is not None:
                    # Detect structure and zones for chart
                    from analysis.structure import analyze_structure
                    from analysis.supply_demand import detect_sd_zones
                    structure = analyze_structure(df)
                    zones = detect_sd_zones(df)
                    photo = render_smc_chart(df, symbol, structure, zones, signal=signal)
                
                await self._notify(f"✅ **TRADE EXECUTED**\n\n{format_signal_report(signal)}", photo=photo)
                logger.info(f"Trade executed: {symbol} {signal.direction} score={signal.score:.1f}")
                return True
            else:
                self.telemetry.increment("orders_rejected")
                self.telemetry.record_rejection(f"Broker rejected order: {result.message}")
                if signal.setup_id is not None:
                    await db.update_setup_record(signal.setup_id, status="execution_failed", rejection_reason=result.message)
                    await db.record_execution_event(
                        account_mode=self.settings.trading_mode,
                        symbol=symbol,
                        setup_id=signal.setup_id,
                        requested_price=signal.entry_price,
                        execution_delay_ms=(perf_counter() - execution_started) * 1000,
                        status="rejected",
                        reason=result.message,
                    )
                await self._chart_activity(
                    "broker_rejected", symbol,
                    f"❌ **BROKER ORDER REJECTED — {symbol}**\nReason: {result.message}\nNo position was opened.",
                    fingerprint=f"{setup_id}:broker:{result.message}", essential=True,
                )
                return False

        except Exception as e:
            logger.error(f"Error executing signal for {symbol}: {e}", exc_info=True)
            raise

    async def scan_and_execute(self):
        """Run at most one real market scan across every trigger source at a time."""
        if self._scan_lock.locked():
            self.telemetry.increment("scan_cycles_skipped_overlap")
            logger.warning("[SCANNER SKIPPED] reason=overlap active_scan_started=%s", self.telemetry.snapshot().get("components", {}).get("market_scanner", {}).get("last_started"))
            return {"skipped": "scan already running"}
        async with self._scan_lock:
            started = perf_counter()
            self.telemetry.component_started("market_scanner")
            self.telemetry.increment("scan_cycles_started")
            logger.info("[SCANNER START] timestamp=%s", datetime.utcnow().isoformat())
            try:
                result = await self._scan_and_execute()
            except Exception as exc:
                self.telemetry.increment("scan_cycles_failed")
                self.telemetry.component_failed("market_scanner", exc)
                logger.exception("[SCANNER FAILURE] duration=%.3fs", perf_counter() - started)
                raise
            self.telemetry.increment("scan_cycles_completed")
            self.telemetry.component_succeeded("market_scanner", waiting=False)
            logger.info("[SCANNER COMPLETE] timestamp=%s duration=%.3fs", datetime.utcnow().isoformat(), perf_counter() - started)
            return result

    async def _scan_and_execute(self):
        """Main scan implementation: validate account, manage positions, then scan usable markets."""
        await self._reload_settings()
        self.capital_state_service.settings = self.settings
        self.capital_state_service.executor = self.executor
        capital = await self.capital_state_service.evaluate()
        self.last_capital_state = capital
        self._set_analysis_eligible_symbols(capital.get("broker_metadata") or {})
        if capital.get("changed"):
            await self._notify_capital_state(capital)
        if capital.get("state") in AccountCapitalState.BLOCKING:
            if not self.settings.is_paused:
                self.settings.is_paused = True
                await db.save_settings(self.settings)
            logger.warning("Scan halted by authoritative account state: %s (%s)", capital.get("state"), capital.get("reason"))
            return

        # ─── ACTIVE TRADE MANAGEMENT ──────────────────────
        # We manage positions even if auto_trade is OFF (to protect existing trades)
        if not self.settings.is_paused:
            try:
                await self.manage_open_positions()
            except Exception as e:
                logger.error(f"Error managing positions: {e}")

        capital_session = await db.get_active_capital_reduction_session("demo")
        if capital_session:
            logger.info("Capital reduction session #%s is %s — normal strategy scanning is suspended", capital_session["id"], capital_session["status"])
            return

        if not self.settings.auto_trade or self.settings.is_paused:
            logger.debug("Auto-trade disabled or paused — skipping scan")
            return
            
        audit = capital.get("broker_metadata") or {}
        scan_symbols = list(audit.get("usable_symbols") or [])
        if not scan_symbols:
            logger.warning("No broker-validated usable Deriv targets are active; skipping scan")
            return

        logger.info("[SCANNER TARGETS] universe=%s usable=%s scanner_received=%s", audit.get("target_count", 0), audit.get("usable_count", 0), len(scan_symbols))
        for symbol in scan_symbols:
            self.telemetry.increment("symbols_attempted")
            logger.info("[SYMBOL LOOP START] %s", symbol)
            try:
                # Session participation is intentionally not a global execution
                # gate. A candidate policy may later include a session feature
                # when broker-realized evidence supports it.
                # For the background loop, we analyze and execute.
                self.telemetry.component_started("analysis_engine")
                try:
                    signal = await self.analyze_symbol(symbol)
                except Exception as exc:
                    self.telemetry.increment("analysis_failures")
                    self.telemetry.component_failed("analysis_engine", exc)
                    raise
                self.telemetry.component_succeeded("analysis_engine", waiting=not bool(signal and signal.passed))
                if not signal or not signal.passed:
                    continue
                self.telemetry.increment("setups_detected")
                self.telemetry.increment("trade_candidates")
                
                await self._chart_activity(
                    "execution_queue", symbol,
                    f"📋 **VALIDATED SETUP QUEUED — {symbol}**\nDirection: `{signal.direction}` | Quality rank: `{signal.score:.1f}/100`\nThe bot is beginning final revalidation and broker risk checks.",
                    fingerprint=f"{signal.setup_id}:{signal.direction}:{signal.entry_price}",
                )

                # Fetch data for the chart if signal passed
                primary_tf = self.settings.timeframes[0] if self.settings.timeframes else "M15"
                df = await self.fetch_candles(symbol, primary_tf, 500)
                
                # Execute
                await self.execute_signal(signal, df)

            except Exception as e:
                self.telemetry.record_error(f"symbol {symbol}: {type(e).__name__}: {e}")
                logger.error(f"Error processing {symbol}: {e}", exc_info=True)

    async def run_self_optimization(self):
        """Run one bounded champion/challenger cycle and retain an audit trail."""
        self.telemetry.component_started("learning_engine")
        logger.info("Running bounded walk-forward optimization...")
        try:
            self.optimizer.settings = self.settings
            rollback = await self.optimizer.evaluate_rollback(self.settings.trading_mode)
            result = rollback or await self.optimizer.run_optimization(self.settings.trading_mode)
        except Exception as exc:
            self.telemetry.component_failed("learning_engine", exc)
            raise
        self.telemetry.increment("optimization_runs")
        self.telemetry.increment("experiments")
        self.telemetry.component_succeeded("learning_engine", waiting=True)
        if result.get("decision") in {"promoted", "rolled_back"}:
            await self._notify(
                "🧠 **MODEL GOVERNANCE UPDATE**\n"
                f"Decision: `{result['decision']}`\n"
                f"Reason: {result.get('reason', 'Measured post-promotion performance required a rollback.')}"
            )

    async def send_daily_journal(self):
        """Generate and send the readable, factual morning learning report."""
        self.telemetry.component_started("learning_engine")
        logger.info("Generating daily learning report...")
        try:
            self.optimizer.settings = self.settings
            journal = await self.optimizer.generate_daily_journal(self.settings.trading_mode)
            await self._notify(journal)
        except Exception as exc:
            self.telemetry.component_failed("learning_engine", exc)
            raise
        self.telemetry.component_succeeded("learning_engine", waiting=True)

    async def _chart_activity(
        self,
        stage: str,
        symbol: str,
        message: str,
        *,
        fingerprint: str,
        essential: bool = False,
        photo: bytes = None,
    ) -> bool:
        """Send one deduplicated chart-state event after a closed-candle decision.

        ``detailed`` reports study and validation stages. ``essential`` reports
        only execution-critical events. A symbol/stage event is emitted once per
        fingerprint; a short time throttle protects Telegram from M1 bursts.
        """
        if not self.settings.chart_activity_notifications:
            return False
        level = self.settings.chart_activity_level
        if level == "off" or (level == "essential" and not essential):
            return False
        if stage == "validation_rejected" and not self.settings.chart_activity_include_rejections:
            return False

        key = f"{symbol}:{stage}"
        now = monotonic()
        prior = self._chart_activity_ledger.get(key)
        cooldown = max(30, int(self.settings.chart_activity_cooldown_seconds))
        if prior and prior[0] == fingerprint:
            return False
        if prior and now - prior[1] < cooldown and not essential:
            return False
        self._chart_activity_ledger[key] = (fingerprint, now)
        await self._notify(message, photo=photo, include_whatsapp=essential)
        return True

    async def _notify(self, message: str, photo: bytes = None, *, include_whatsapp: bool = True):
        """Send notification to Telegram and, for material events, WhatsApp."""
        # Telegram
        if self.bot_app and self.admin_chat_id:
            try:
                if photo:
                    await self.bot_app.bot.send_photo(self.admin_chat_id, photo, caption=message)
                else:
                    await self.bot_app.bot.send_message(self.admin_chat_id, message)
            except Exception as e:
                logger.error(f"Failed to send Telegram notification: {e}")
        
        # Chart-study detail stays on Telegram. WhatsApp receives material
        # execution, safety, and management events only.
        if not include_whatsapp:
            return

        # WhatsApp (via CallMeBot relay)
        wa_phone = os.getenv("WHATSAPP_PHONE")
        wa_apikey = os.getenv("WHATSAPP_APIKEY")
        if wa_phone and wa_apikey:
            try:
                import requests
                import urllib.parse
                clean_msg = message.replace("**", "").replace("`", "")
                encoded_msg = urllib.parse.quote(clean_msg)
                url = f"https://api.callmebot.com/whatsapp.php?phone={wa_phone}&text={encoded_msg}&apikey={wa_apikey}"
                requests.get(url, timeout=10)
            except Exception as e:
                logger.error(f"Failed to send WhatsApp notification: {e}")

    async def _reconcile_closed_trades(self, live_tickets: set[int]) -> None:
        """Close local learning records only after broker history confirms an outcome."""
        for trade in await db.get_open_trades(self.settings.trading_mode):
            ticket = trade.get("ticket")
            if ticket is None or int(ticket) in live_tickets:
                continue
            outcome = await self.executor.get_closed_position_outcome(int(ticket))
            if not outcome:
                continue
            initial_risk = float(trade.get("initial_risk") or 0.0)
            pnl = float(outcome.get("pnl") or 0.0)
            pnl_r = pnl / initial_risk if initial_risk > 0 else None
            pnl_r_text = f"{pnl_r:.2f}R" if pnl_r is not None else "N/A"
            await db.close_trade(
                int(trade["id"]),
                pnl,
                exit_price=float(outcome.get("exit_price") or 0.0),
                pnl_r=pnl_r,
                max_favorable_r=float(trade.get("max_favorable_r") or 0.0),
                max_adverse_r=float(trade.get("max_adverse_r") or 0.0),
            )
            await self._chart_activity(
                "broker_exit", trade["symbol"],
                f"🏁 **BROKER EXIT CONFIRMED — {trade['symbol']}**\nTicket: `#{ticket}` | Realized P/L: `${pnl:.2f}` | Result: `{pnl_r_text}`\nExit price: `{float(outcome.get('exit_price') or 0.0):.5f}` | MFE: `{float(trade.get('max_favorable_r') or 0.0):.2f}R` | MAE: `{float(trade.get('max_adverse_r') or 0.0):.2f}R`",
                fingerprint=f"{ticket}:{pnl}:{outcome.get('exit_price')}", essential=True,
            )
            setup_id = trade.get("setup_id")
            if setup_id:
                await db.update_setup_record(
                    int(setup_id),
                    status="closed",
                    outcome={
                        "pnl": pnl,
                        "pnl_r": pnl_r,
                        "exit_price": outcome.get("exit_price"),
                        "max_favorable_r": trade.get("max_favorable_r", 0.0),
                        "max_adverse_r": trade.get("max_adverse_r", 0.0),
                    },
                )

    async def manage_open_positions(self):
        """Instrument real position-management checks and broker outcomes."""
        self.telemetry.component_started("position_manager")
        try:
            result = await self._manage_open_positions()
        except Exception as exc:
            self.telemetry.component_failed("position_manager", exc)
            raise
        self.telemetry.component_succeeded("position_manager", waiting=bool(result == 0))
        return result

    async def _manage_open_positions(self):
        """Manage each open trade from fresh closed-candle structure and basket state."""
        positions = await self.executor.get_open_positions()
        self.telemetry.increment("positions_checked", len(positions))
        try:
            live_tickets = {position.ticket for position in positions}
            await self._reconcile_closed_trades(live_tickets)
            if not positions:
                return 0
            logger.info("Managing %s open position(s) using their recorded experimental policies", len(positions))

            for position in positions:
                basket = await db.get_basket_for_ticket(position.ticket, self.settings.trading_mode)
                if basket:
                    initial_stop = float(basket["initial_stop"])
                    try:
                        state = ManagementState(basket["state"])
                    except ValueError:
                        state = ManagementState.INITIAL
                    partial_done = await db.basket_has_action(basket["id"], "Partial Take Profit")
                    policy_data = dict(basket.get("metadata", {}).get("experimental_policy") or {})
                else:
                    # Manual positions are monitored defensively, but the bot
                    # will not create layers without a recorded basket plan.
                    initial_stop = position.sl
                    state = ManagementState.INITIAL
                    partial_done = False
                    active_policy, _, _ = await self.optimizer.active_policy(self.settings.trading_mode)
                    policy_data = active_policy.to_dict()

                manager = TradeManager(
                    policy=policy_data,
                    min_sl_update_distance=self.settings.min_sl_update_distance_atr,
                    min_tp_update_distance=self.settings.min_tp_update_distance_atr,
                )

                if initial_stop <= 0:
                    logger.warning("Skipping unprotected position #%s; no initial structural stop is known", position.ticket)
                    continue

                df = await self.fetch_candles(position.symbol, "M5", 200)
                if df.empty or len(df) < 30:
                    continue
                current_price = float(df.iloc[-1]["close"])
                if basket and basket.get("metadata", {}).get("trade_id"):
                    initial_distance = abs(position.entry_price - initial_stop)
                    current_r = (
                        ((current_price - position.entry_price) if position.direction == "BUY" else (position.entry_price - current_price))
                        / initial_distance
                        if initial_distance > 0
                        else 0.0
                    )
                    await db.update_trade_excursions(int(basket["metadata"]["trade_id"]), current_r=current_r)
                atr_val = float(atr(df, 14).iloc[-1])
                if atr_val <= 0:
                    continue

                structure = analyze_structure(df, lookback=3)
                pools = build_liquidity_pools(df, structure.swing_highs, structure.swing_lows, "M5")
                target_pool = select_market_target(pools, position.direction, position.entry_price)
                structural_target = target_pool.level if target_pool else None
                action = manager.evaluate(
                    direction=position.direction,
                    entry_price=position.entry_price,
                    initial_stop=initial_stop,
                    current_sl=position.sl,
                    current_tp=position.tp,
                    current_price=current_price,
                    atr_value=atr_val,
                    structure=structure,
                    state=state,
                    partial_exit_done=partial_done,
                    structural_target=structural_target,
                    costs_buffer=atr_val * 0.02,
                )
                if action.action == "none":
                    continue

                if action.action == "move_sl" and action.new_sl is not None:
                    success = await self.executor.modify_position(position.ticket, sl=action.new_sl, tp=position.tp)
                    if success:
                        self.telemetry.increment("positions_modified")
                        if basket:
                            await db.update_basket_state(basket["id"], state=action.state.value)
                            await db.update_trade_layer(basket["layer_id"], stop_loss=action.new_sl)
                            await db.log_basket_action(
                                basket_id=basket["id"],
                                ticket=position.ticket,
                                action="SL Protected",
                                details={"old_sl": position.sl, "new_sl": action.new_sl, "current_r": manager.current_r(position.direction, position.entry_price, initial_stop, current_price), "reason": action.reason},
                            )
                        else:
                            await db.log_trade_action(position.ticket, "SL Protected", action.reason)
                        await self._notify(f"🛡 **SL PROTECTED — {position.symbol}**\nTicket: `#{position.ticket}`\nOld SL: `{position.sl:.5f}`\nNew SL: `{action.new_sl:.5f}`\nReason: _{action.reason}_")

                elif action.action == "move_tp" and action.new_tp is not None:
                    success = await self.executor.modify_position(position.ticket, sl=position.sl, tp=action.new_tp)
                    if success:
                        self.telemetry.increment("positions_modified")
                        if basket:
                            await db.update_basket_state(basket["id"], state=action.state.value)
                            await db.update_trade_layer(basket["layer_id"], take_profit=action.new_tp)
                            await db.log_basket_action(
                                basket_id=basket["id"],
                                ticket=position.ticket,
                                action="TP Extended",
                                details={"old_tp": position.tp, "new_tp": action.new_tp, "reason": action.reason},
                            )
                        else:
                            await db.log_trade_action(position.ticket, "TP Extended", action.reason)
                        await self._notify(f"🎯 **TP EXTENDED — {position.symbol}**\nTicket: `#{position.ticket}`\nOld TP: `{position.tp:.5f}`\nNew TP: `{action.new_tp:.5f}`\nReason: _{action.reason}_")

                elif action.action == "close_partial" and action.close_percent:
                    sym_info = await self.executor.get_symbol_info(position.symbol)
                    close_volume = self.risk_manager.floor_volume(position.volume * action.close_percent, sym_info)
                    if close_volume > 0 and await self.executor.close_partial(position.ticket, close_volume):
                        if basket:
                            await db.update_basket_state(basket["id"], state=action.state.value)
                            await db.log_basket_action(
                                basket_id=basket["id"],
                                ticket=position.ticket,
                                action="Partial Take Profit",
                                details={"volume": close_volume, "percent": action.close_percent, "reason": action.reason},
                            )
                        else:
                            await db.log_trade_action(position.ticket, "Partial Take Profit", action.reason)
                        await self._notify(f"💰 **PARTIAL TAKE PROFIT — {position.symbol}**\nTicket: `#{position.ticket}`\nClosed: `{close_volume}` lots\nReason: _{action.reason}_")

                elif action.action == "close_full":
                    if await self.executor.close_position(position.ticket):
                        self.telemetry.increment("positions_closed")
                        if basket:
                            await db.update_basket_state(basket["id"], state=action.state.value, status="closed")
                            await db.log_basket_action(
                                basket_id=basket["id"],
                                ticket=position.ticket,
                                action="Thesis Exit",
                                details={"reason": action.reason},
                            )
                        else:
                            await db.log_trade_action(position.ticket, "Thesis Exit", action.reason)
                        await self._notify(f"⚠️ **TRADE THESIS EXIT — {position.symbol}**\nTicket: `#{position.ticket}`\nReason: _{action.reason}_")

                # A planned layer is evaluated independently after current
                # management. The method refuses losing/duplicate/invalid adds.
                if basket and action.action != "close_full":
                    await self.maybe_add_confirmed_layer(basket, position, positions)

            for basket in await db.get_open_baskets(self.settings.trading_mode):
                await db.close_basket_if_flat(basket["id"], live_tickets)
            return len(positions)
        except Exception as e:
            logger.error(f"Error in manage_open_positions: {e}", exc_info=True)
            raise

    async def maybe_add_confirmed_layer(self, basket: dict, position, all_positions: list) -> bool:
        """Add at most one planned layer after fresh confirmation, never while losing.

        A layer is treated as a continuation/retest decision, not a cheaper
        re-entry. It is blocked unless the current basket is in profit, a new
        valid setup is present, the structural event is new, risk remains inside
        the original setup budget, and free margin supports the reduced volume.
        """
        if not self.settings.auto_trade or self.settings.is_paused:
            return False
        layers = await db.get_basket_layers(basket["id"])
        next_layer = next((layer for layer in layers if layer["status"] == "planned"), None)
        if not next_layer:
            return False

        primary_ticket = min((layer["ticket"] for layer in layers if layer.get("ticket")), default=position.ticket)
        if position.ticket != primary_ticket:
            return False
        initial_stop = float(basket["initial_stop"])
        policy_data = dict(basket.get("metadata", {}).get("experimental_policy") or {})
        layer_style = str(policy_data.get("layer_style", "none"))
        current_price_df = await self.fetch_candles(position.symbol, "M5", 200)
        if current_price_df.empty:
            return False
        current_price = float(current_price_df.iloc[-1]["close"])
        initial_risk = abs(position.entry_price - initial_stop)
        current_r = ((current_price - position.entry_price) if position.direction == "BUY" else (position.entry_price - current_price)) / initial_risk if initial_risk > 0 else 0.0
        if current_r <= 0 and layer_style not in {"averaging", "retracement"}:
            return False  # The selected policy has not elected adverse/pullback scaling.

        refreshed = await self.analyze_symbol(position.symbol)
        if not refreshed or not refreshed.passed or refreshed.direction != basket["direction"]:
            return False
        event_key = f"{refreshed.structure.last_event.event_type.value}:{refreshed.structure.last_event.index}"
        metadata = dict(basket.get("metadata") or {})
        if metadata.get("last_layer_event") == event_key:
            return False  # Duplicate-layer prevention for an already-used event.

        account = await self.executor.get_account_info()
        equity = float(account.get("equity", account.get("balance", 0)))
        free_margin = float(account.get("free_margin", 0))
        leverage = float(account.get("leverage", 1) or 1)
        symbol_info = await self._execution_symbol_spec(position.symbol, refreshed.direction)

        basket_tickets = {layer["ticket"] for layer in layers if layer.get("ticket")}
        basket_current_risk = 0.0
        total_open_risk = 0.0
        for live_position in all_positions:
            live_info = await self.executor.get_symbol_info(live_position.symbol)
            risk = self.risk_manager.calculate_position_risk(live_position, live_info)
            if risk == float("inf"):
                return False
            total_open_risk += risk
            if live_position.ticket in basket_tickets:
                basket_current_risk += risk

        remaining_basket_risk = max(0.0, float(basket["max_risk"]) - basket_current_risk)
        planned_allocation = next_layer["planned_volume"] / max(sum(layer["planned_volume"] for layer in layers), 1e-12)
        layer_budget = min(float(basket["max_risk"]) * planned_allocation, remaining_basket_risk)
        if layer_budget <= 0:
            return False
        risk_pct = layer_budget / max(equity, 1e-12) * 100
        sizing = self.risk_manager.calculate_position_sizing(
            account_equity=equity,
            free_margin=free_margin,
            entry_price=refreshed.entry_price,
            stop_loss=refreshed.stop_loss,
            symbol_info=symbol_info,
            leverage=leverage,
            risk_pct=risk_pct,
        )
        if not sizing.valid:
            self.telemetry.record_rejection(f"Layer sizing rejected: {sizing.reason}")
            logger.info("[LAYER SIZING REJECTED] symbol=%s code=%s evidence=%s", position.symbol, sizing.sizing_code, sizing.evidence())
            return False
        if sizing.expected_loss > remaining_basket_risk + 1e-6:
            self.telemetry.record_rejection("Layer required lot exceeds remaining basket risk")
            return False

        today_pnl = await db.get_today_pnl(self.settings.trading_mode)
        today_count = await db.get_today_trade_count(self.settings.trading_mode)
        consecutive_losses = await db.get_consecutive_losses(account_mode=self.settings.trading_mode)
        risk_result = await self.risk_manager.check_all(
            symbol=position.symbol,
            direction=refreshed.direction,
            score=refreshed.score,
            rr_ratio=refreshed.rr_ratio,
            spread_pips=0.0,
            account_equity=equity,
            free_margin=free_margin,
            required_margin=sizing.required_margin,
            today_pnl=today_pnl,
            today_trade_count=today_count,
            open_position_count=len(all_positions),
            proposed_setup_risk=sizing.expected_loss,
            current_open_risk=total_open_risk,
            setup_valid=bool(refreshed.validation and refreshed.validation.valid),
            is_layer=True,
            consecutive_losses=consecutive_losses,
            policy=policy_data,
        )
        if not risk_result.passed:
            return False

        self.telemetry.increment("orders_submitted")
        result = await self.executor.execute_trade(
            symbol=position.symbol,
            direction=refreshed.direction,
            lot_size=sizing.final_volume,
            sl=refreshed.stop_loss,
            tp=refreshed.take_profit,
            magic=self.settings.magic_number,
            comment=f"EXP L{next_layer['layer_number']}/{len(layers)}",
        )
        if not result.success:
            self.telemetry.increment("orders_rejected")
            self.telemetry.record_rejection("Broker rejected confirmed layer")
            return False
        self.telemetry.increment("orders_filled")

        await db.update_trade_layer(
            next_layer["id"],
            status="open",
            ticket=result.ticket,
            executed_volume=result.lot_size,
            stop_loss=result.sl,
            take_profit=result.tp,
            trigger_reason=f"{next_layer['trigger']} — {event_key}",
        )
        metadata["last_layer_event"] = event_key
        await db.update_basket_state(basket["id"], state=ManagementState.CONFIRMED.value, metadata=metadata)
        if result.ticket is not None:
            await db.log_basket_action(
                basket_id=basket["id"],
                ticket=result.ticket,
                action="Layer Added",
                details={
                    "layer": next_layer["layer_number"],
                    "volume": result.lot_size,
                    "layer_risk": sizing.expected_loss,
                    "basket_current_risk": basket_current_risk + sizing.expected_loss,
                    "remaining_risk": max(0.0, float(basket["max_risk"]) - basket_current_risk - sizing.expected_loss),
                    "reason": next_layer["trigger"],
                },
            )
        await self._notify(
            f"🟢 **LAYER ADDED — {position.symbol}**\n"
            f"Layer: `{next_layer['layer_number']}/{len(layers)}`\n"
            f"Volume: `{result.lot_size}`\n"
            f"Basket risk: `${basket_current_risk + sizing.expected_loss:.2f}` / `${float(basket['max_risk']):.2f}`\n"
            f"Reason: _{next_layer['trigger']}_"
        )
        return True

    async def manual_manage_position(self, ticket: int) -> str:
        """Re-analyse one position using the same structural safety engine as automation."""
        positions = await self.executor.get_open_positions()
        position = next((item for item in positions if item.ticket == ticket), None)
        if not position:
            return f"❌ Ticket `#{ticket}` not found in open positions."

        basket = await db.get_basket_for_ticket(ticket)
        initial_stop = float(basket["initial_stop"]) if basket else position.sl
        if initial_stop <= 0:
            return f"❌ Ticket `#{ticket}` has no recorded protective stop, so safe R-based management is unavailable."

        df = await self.fetch_candles(position.symbol, "M5", 200)
        if df.empty or len(df) < 30:
            return f"❌ Could not fetch sufficient closed M5 data for {position.symbol}."
        current_price = float(df.iloc[-1]["close"])
        atr_val = float(atr(df, 14).iloc[-1])
        structure = analyze_structure(df, lookback=3)
        pools = build_liquidity_pools(df, structure.swing_highs, structure.swing_lows, "M5")
        target_pool = select_market_target(pools, position.direction, position.entry_price)

        try:
            state = ManagementState(basket["state"]) if basket else ManagementState.INITIAL
        except ValueError:
            state = ManagementState.INITIAL
        partial_done = await db.basket_has_action(basket["id"], "Partial Take Profit") if basket else False
        if basket:
            policy_data = dict(basket.get("metadata", {}).get("experimental_policy") or {})
        else:
            active_policy, _, _ = await self.optimizer.active_policy(self.settings.trading_mode)
            policy_data = active_policy.to_dict()
        manager = TradeManager(
            policy=policy_data,
            min_sl_update_distance=self.settings.min_sl_update_distance_atr,
            min_tp_update_distance=self.settings.min_tp_update_distance_atr,
        )
        action = manager.evaluate(
            direction=position.direction,
            entry_price=position.entry_price,
            initial_stop=initial_stop,
            current_sl=position.sl,
            current_tp=position.tp,
            current_price=current_price,
            atr_value=atr_val,
            structure=structure,
            state=state,
            partial_exit_done=partial_done,
            structural_target=target_pool.level if target_pool else None,
            costs_buffer=atr_val * 0.02,
        )
        current_r = manager.current_r(position.direction, position.entry_price, initial_stop, current_price)
        if action.action == "none":
            return f"ℹ️ **Position #{ticket} Reviewed**\nSymbol: `{position.symbol}`\nCurrent R: `{current_r:.2f}`\nNo material structure-backed SL/TP improvement is justified."
        if action.action == "close_full":
            return f"⚠️ **Position #{ticket} Thesis Warning**\nSymbol: `{position.symbol}`\nCurrent R: `{current_r:.2f}`\n_{action.reason}_\n\nUse `/close {ticket}` if you want to exit manually; the autonomous manager will continue to monitor it."
        if action.action == "close_partial":
            return f"ℹ️ **Position #{ticket} Reviewed**\nA partial realization is eligible at `{current_r:.2f}R`, but no manual partial order was sent by this command."

        new_sl = action.new_sl if action.action == "move_sl" else position.sl
        new_tp = action.new_tp if action.action == "move_tp" else position.tp
        if not await self.executor.modify_position(ticket, sl=new_sl, tp=new_tp):
            return f"❌ MT5 rejected the proposed optimization for ticket `#{ticket}`. The existing SL/TP remains unchanged."

        if basket:
            await db.update_basket_state(basket["id"], state=action.state.value)
            await db.update_trade_layer(basket["layer_id"], stop_loss=new_sl, take_profit=new_tp)
            await db.log_basket_action(
                basket_id=basket["id"],
                ticket=ticket,
                action="Manual Structural Optimization",
                details={"old_sl": position.sl, "new_sl": new_sl, "old_tp": position.tp, "new_tp": new_tp, "current_r": current_r, "reason": action.reason},
            )
        else:
            await db.log_trade_action(ticket, "Manual Structural Optimization", action.reason)
        return (
            f"✅ **Position #{ticket} Optimized**\n"
            f"Symbol: `{position.symbol}`\n"
            f"Current R: `{current_r:.2f}`\n"
            f"SL: `{position.sl:.5f}` → `{new_sl:.5f}`\n"
            f"TP: `{position.tp:.5f}` → `{new_tp:.5f}`\n\n"
            f"Reason: _{action.reason}_"
        )

    async def _reload_settings(self):
        """Reload settings from DB."""
        self.settings = await db.load_settings()
        self.risk_manager.settings = self.settings
