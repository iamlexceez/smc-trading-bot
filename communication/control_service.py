"""Command implementations shared by communication adapters.

These handlers read and mutate the authoritative settings/scheduler objects; the
communication platforms only provide transport and presentation.
"""

from __future__ import annotations

from typing import Any

from config import get_admin_ids
from storage import db

from .command_bus import CommandBus, CommandRequest, CommandResponse
from .events import DeliveryChannel


class SharedControlService:
    def __init__(self, settings: Any, scheduler: Any, *, db_path: str | None = None):
        self.settings = settings
        self.scheduler = scheduler
        self.db_path = db_path

    def authorize(self, request: CommandRequest, dangerous: bool) -> bool:
        if request.platform == "telegram":
            return str(request.user_id) in {str(value) for value in get_admin_ids()}
        import os

        allowed_users = {
            value.strip()
            for value in os.getenv("SLACK_ALLOWED_USER_IDS", "").split(",")
            if value.strip()
        }
        allowed_channels = {
            value.strip()
            for value in os.getenv("SLACK_ALLOWED_CHANNEL_IDS", "").split(",")
            if value.strip()
        }
        # Fail closed for Slack control until the owner explicitly configures
        # the Slack user allow-list. If channel IDs are configured, require
        # both the user and channel to be approved.
        return (
            str(request.user_id) in allowed_users
            and (not allowed_channels or str(request.channel_id) in allowed_channels)
        )

    async def status(self, request: CommandRequest) -> str:
        gate = dict(getattr(self.scheduler, "last_scan_gate", {}) or {})
        return "\n".join([
            "📊 SMC BOT STATUS",
            f"Mode: {str(self.settings.trading_mode).upper()}",
            f"Auto-trade: {'ON' if self.settings.auto_trade else 'OFF'}",
            f"Paused: {'YES' if self.settings.is_paused else 'NO'}",
            f"Scan gate: {gate.get('state', 'UNKNOWN')}",
            f"Gate reason: {gate.get('reason', 'none')}",
            f"Broker-usable symbols: {gate.get('broker_usable_symbols', 'UNKNOWN')}",
        ])

    async def health(self, request: CommandRequest) -> str:
        connected = False
        try:
            connected = bool(self.scheduler.executor and await self.scheduler.executor.is_connected())
        except Exception:
            connected = False
        return "\n".join([
            "🩺 SMC BOT HEALTH",
            f"MT5: {'CONNECTED' if connected else 'DISCONNECTED'}",
            f"Scheduler: {'RUNNING' if getattr(self.scheduler, '_running', False) else 'STOPPED'}",
            f"Telegram adapter: {'READY' if getattr(self.scheduler, 'bot_app', None) else 'NOT CONFIGURED'}",
            f"Slack webhook: {'CONFIGURED' if getattr(self.scheduler.notification_manager.adapters.get(DeliveryChannel.SLACK), 'webhook_url', '') else 'NOT CONFIGURED'}",
            f"Slack control: {'CONFIGURED' if self._slack_control_configured() else 'NOT CONFIGURED'}",
        ])

    async def engine(self, request: CommandRequest) -> str:
        runtime = self.scheduler.telemetry.snapshot(include_lifetime=True)
        components = runtime.get("components") or {}
        tracker = getattr(self.scheduler, "invocation_tracker", None)
        invocation = tracker.summary() if tracker is not None else {}
        return "\n".join([
            "ENGINE",
            f"Market scanner: {(components.get('market_scanner') or {}).get('state', 'UNKNOWN')}",
            f"Analysis: {(components.get('analysis_engine') or {}).get('state', 'UNKNOWN')}",
            f"Execution: {(components.get('execution_engine') or {}).get('state', 'UNKNOWN')}",
            f"Position manager: {(components.get('position_manager') or {}).get('state', 'UNKNOWN')}",
            f"Learning: {(components.get('learning_engine') or {}).get('state', 'UNKNOWN')}",
            f"Setup Intelligence V2: {'RUNNING' if (invocation.get('rows') and any(r['module_name'] == 'analysis.setup_intelligence' and r['called'] for r in invocation['rows'])) else 'INITIALIZED'}",
            f"Legacy Setup Authority: DISABLED",
            f"Last scan: {getattr(self.scheduler, '_last_scan_disposition', {}).get('state', 'UNKNOWN')}",
            f"Invocation matrix: {invocation.get('complete_modules', 0)}/{invocation.get('total_modules', 0)} complete",
        ])

    async def positions(self, request: CommandRequest) -> str:
        try:
            positions = await self.scheduler.executor.get_open_positions()
        except Exception as exc:
            return f"POSITIONS UNAVAILABLE\nReason: {type(exc).__name__}"
        if not positions:
            return "OPEN POSITIONS\nNone"
        lines = ["OPEN POSITIONS"]
        for position in positions[:20]:
            if isinstance(position, dict):
                symbol = position.get("symbol", "?")
                ticket = position.get("ticket", "?")
                pnl = position.get("profit", 0.0)
            else:
                symbol = getattr(position, "symbol", "?")
                ticket = getattr(position, "ticket", "?")
                pnl = getattr(position, "profit", 0.0)
            lines.append(f"#{ticket} {symbol} P/L={float(pnl or 0):.2f}")
        return "\n".join(lines)

    async def opportunities(self, request: CommandRequest) -> str:
        ranking = list(getattr(self.scheduler, "last_opportunity_ranking", []) or [])
        if not ranking:
            return "OPPORTUNITIES\nNo ranked opportunity is available."
        lines = ["TOP OPPORTUNITIES"]
        for index, item in enumerate(ranking[:5], 1):
            details = dict(item.get("details") or {})
            lines.append(
                f"{index}. {item.get('symbol', '?')} {item.get('direction', '?')} "
                f"score={float(item.get('score') or 0):.1f} "
                f"evidence={details.get('evidence_classification', 'UNKNOWN')}"
            )
        return "\n".join(lines)

    async def core(self, request: CommandRequest) -> str:
        governance = dict(getattr(self.scheduler, "last_research_governance", {}) or {})
        specialization = dict(governance.get("instrument_specialization") or {})
        core_symbols = list(specialization.get("core_symbols") or [])
        rankings = {str(row.get("instrument")): row for row in specialization.get("rankings", [])}
        lines = ["CORE INSTRUMENT REVIEW"]
        if core_symbols:
            for index, symbol in enumerate(core_symbols, 1):
                row = rankings.get(str(symbol), {})
                score = dict(row.get("specialization") or {}).get("adjusted_score")
                lines.append(f"{index}. {symbol} | {row.get('role', 'CORE')}")
                lines.append(f"   Trades: {row.get('sample_size', 0)} | Expectancy: {float(row.get('expectancy_r') or 0.0):+.2f}R")
                lines.append(f"   Win Rate: {float(row.get('win_rate') or 0.0)*100:.1f}% | Max DD: {float(row.get('max_drawdown_r') or 0.0):.2f}R")
                lines.append(f"   Score: {float(score or 0.0):.2f}/100")
        else:
            lines.append("No instrument currently qualifies for CORE.")
            lines.append(str(specialization.get("core_selection_explanation") or "Fresh broker-verified specialization evidence is not yet sufficient."))
        
        non_core = [row for row in specialization.get("rankings", []) if not row.get("selected_core")]
        if non_core:
            lines.append("")
            lines.append("CHALLENGERS / EXPLORATORY")
            for row in non_core[:5]:
                score = dict(row.get("specialization") or {}).get("adjusted_score")
                lines.append(f"{row.get('instrument', '?')} | {row.get('role', 'RESEARCH')} | Score: {float(score or 0.0):.2f}")
        return "\n".join(lines)

    async def learning(self, request: CommandRequest) -> str:
        kwargs = {"db_path": self.db_path} if self.db_path else {}
        rows = await db.get_strategy_evidence_summary(self.settings.trading_mode, days=30, **kwargs)
        perf = await db.get_performance_summary(self.settings.trading_mode, **kwargs)
        
        total_trades = int(perf.get("trades", 0))
        if total_trades < 25: phase = "EXPLORATION"
        elif total_trades < 100: phase = "DEVELOPING"
        else: phase = "MATURE"
        
        lines = [
            "LEARNING UPDATE",
            f"Phase: {phase}",
            f"Completed MT5 trades: {total_trades}",
            f"Current DEMO floor: {self.settings.normal_demo_min_setup_score:.1f}",
            f"Experimental floor: {self.settings.exploration_min_setup_score:.1f}",
            ""
        ]
        
        if rows:
            best = rows[0]
            lines.append(f"Best setup family: {best.get('strategy_id', 'unknown')}")
            lines.append(f"Best instrument: {best.get('symbol', 'unknown')}")
            lines.append(f"Best expectancy: {best.get('expectancy_r', 'unknown')}R")
            lines.append(f"Evidence strength: {best.get('confidence', 'UNKNOWN')}")
        else:
            lines.append("Insufficient evidence for ranked summary.")
            
        return "\n".join(lines)

    async def scorebuckets(self, request: CommandRequest) -> str:
        kwargs = {"db_path": self.db_path} if self.db_path else {}
        buckets = await db.get_score_bucket_performance(self.settings.trading_mode, **kwargs)
        
        lines = ["SETUP SCORE PERFORMANCE"]
        for label in sorted(buckets.keys()):
            b = buckets[label]
            if b["trades"] > 0:
                lines.append(f"{label}:")
                lines.append(f"   Trades: {b['trades']} | Win Rate: {b['win_rate']:.1f}%")
                lines.append(f"   Expectancy: {b['expectancy']:+.2f}R | PF: {b['profit_factor']:.2f}")
            else:
                lines.append(f"{label}: No trades")
                
        return "\n".join(lines)

    async def research(self, request: CommandRequest) -> str:
        kwargs = {"db_path": self.db_path} if self.db_path else {}
        hypotheses = await db.get_open_hypotheses(self.settings.trading_mode, **kwargs)
        if not hypotheses:
            return "RESEARCH\nNo open hypotheses."
        return "\n".join(["RESEARCH OPEN HYPOTHESES", *[
            f"- {item.get('hypothesis_key', '?')}: {item.get('statement', '未 specified')}"
            for item in hypotheses[:8]
        ]])

    async def experiments(self, request: CommandRequest) -> str:
        kwargs = {"db_path": self.db_path} if self.db_path else {}
        rows = await db.list_policy_experiments(self.settings.trading_mode, limit=8, **kwargs)
        if not rows:
            return "EXPERIMENTS\nNone recorded."
        return "\n".join(["EXPERIMENTS", *[
            f"- #{row.get('id', '?')} {row.get('status', '?')} {row.get('policy_fingerprint', '?')}"
            for row in rows
        ]])

    async def objective(self, request: CommandRequest) -> str:
        kwargs = {"db_path": self.db_path} if self.db_path else {}
        active = await db.get_active_objective(self.settings.trading_mode, **kwargs)
        if not active:
            return "OBJECTIVE\nNo active objective."
        objective = active.get("objective_json") or active.get("objective") or {}
        if isinstance(objective, str):
            import json
            objective = json.loads(objective or "{}")
        return "\n".join([
            "OBJECTIVE",
            f"Status: {active.get('status', 'UNKNOWN')}",
            f"Version: {active.get('version', '?')}",
            f"Target: {objective.get('target_capital', objective.get('target_equity', 'unknown'))}",
            f"Mode: {self.settings.trading_mode.upper()}",
        ])

    async def performance(self, request: CommandRequest) -> str:
        kwargs = {"db_path": self.db_path} if self.db_path else {}
        summary = await db.get_performance_summary(self.settings.trading_mode, days=7, **kwargs)
        return "\n".join([
            "PERFORMANCE — LAST 7 DAYS",
            f"Trades: {summary.get('trades', summary.get('trade_count', 'unknown'))}",
            f"PnL: {summary.get('pnl', summary.get('total_pnl', 'unknown'))}",
            f"Win rate: {summary.get('win_rate', 'unknown')}",
        ])

    async def diagnostics(self, request: CommandRequest) -> str:
        return await self.engine(request)

    async def logs(self, request: CommandRequest) -> str:
        return "LOGS\nRaw logs remain on the VPS in logs\\bot_runtime.log. Use /diagnostics for a safe operational summary."

    async def help(self, request: CommandRequest) -> str:
        return "\n".join([
            "SMC COMMANDS",
            "/status — authoritative bot state",
            "/engine — scanner and subsystem state",
            "/health — broker and communication health",
            "/positions — open broker positions",
            "/opportunities — current ranked opportunities",
            "/pause --confirm TOKEN — halt new trading",
            "/resume --confirm TOKEN — verify broker state and resume",
            "/help — this command list",
        ])

    async def pause(self, request: CommandRequest) -> str:
        self.settings.is_paused = True
        self.settings.automation_pause_reason = f"MANUAL_{request.platform.upper()}"
        kwargs = {"db_path": self.db_path} if self.db_path else {}
        await db.save_settings(self.settings, **kwargs)
        return "⏸ Trading paused. Existing position management remains independent."

    async def resume(self, request: CommandRequest) -> str:
        service = getattr(self.scheduler, "capital_state_service", None)
        result = await service.verify_resume() if service else {"resume_verified": False, "reason": "Account service unavailable"}
        if not result.get("resume_verified"):
            return f"Trading remains halted: {result.get('reason', 'broker state not verified')}"
        if str(self.settings.trading_mode).lower() == "demo":
            self.settings.auto_trade = True
        self.settings.is_paused = False
        self.settings.automation_pause_reason = ""
        kwargs = {"db_path": self.db_path} if self.db_path else {}
        await db.save_settings(self.settings, **kwargs)
        return "▶️ Broker state verified. Trading resumed; the scheduler will continue its normal scan cycle."

    def _slack_control_configured(self) -> bool:
        import os
        return bool(os.getenv("SLACK_APP_TOKEN") and os.getenv("SLACK_BOT_TOKEN") and os.getenv("SLACK_ALLOWED_USER_IDS"))


def build_command_bus(service: SharedControlService) -> CommandBus:
    bus = CommandBus(authorizer=service.authorize)
    bus.register("status", service.status)
    bus.register("engine", service.engine)
    bus.register("health", service.health)
    bus.register("positions", service.positions)
    bus.register("opportunities", service.opportunities)
    bus.register("core", service.core)
    bus.register("learning", service.learning)
    bus.register("research", service.research)
    bus.register("experiments", service.experiments)
    bus.register("objective", service.objective)
    bus.register("performance", service.performance)
    bus.register("scorebuckets", service.scorebuckets)
    bus.register("diagnostics", service.diagnostics)
    bus.register("logs", service.logs)
    bus.register("help", service.help)
    bus.register("pause", service.pause, dangerous=True)
    bus.register("resume", service.resume, dangerous=True)
    return bus
