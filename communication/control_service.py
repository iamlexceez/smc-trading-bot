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
        return "\n".join([
            "ENGINE",
            f"Market scanner: {(components.get('market_scanner') or {}).get('state', 'UNKNOWN')}",
            f"Analysis: {(components.get('analysis_engine') or {}).get('state', 'UNKNOWN')}",
            f"Execution: {(components.get('execution_engine') or {}).get('state', 'UNKNOWN')}",
            f"Position manager: {(components.get('position_manager') or {}).get('state', 'UNKNOWN')}",
            f"Learning: {(components.get('learning_engine') or {}).get('state', 'UNKNOWN')}",
            f"Last scan: {getattr(self.scheduler, '_last_scan_disposition', {}).get('state', 'UNKNOWN')}",
        ])

    async def positions(self, request: CommandRequest) -> str:
        try:
            positions = await self.scheduler.executor.get_positions()
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
    bus.register("help", service.help)
    bus.register("pause", service.pause, dangerous=True)
    bus.register("resume", service.resume, dangerous=True)
    return bus
