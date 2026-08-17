"""
SMC Trading Bot — Main entry point.

A Telegram-controlled trading bot using APA (Advanced Price Action) and
Supply/Demand strategies with multi-factor scoring and auto-execution.

Usage:
    python main.py

Environment:
    Copy .env.example to .env and fill in your details.
    See docs/VPS_DEPLOYMENT.md for VPS setup instructions.
"""

import asyncio
import logging
import sys
import structlog

from config import (
    get_telegram_token, get_admin_ids, get_mt5_credentials,
    get_trading_mode, TradeSettings,
)
from storage import db
from bot.handlers import BotHandlers
from executors.mt5 import MT5Executor, MT5_AVAILABLE
from risk.manager import RiskManager
from scheduler import MarketScheduler
from communication.events import DeliveryChannel, EventSeverity, NotificationEvent
from communication.command_bus import CommandBus
from communication.control_service import SharedControlService, build_command_bus
from communication.slack_control import SlackSocketControl

# ─── Logging ──────────────────────────────────────────────
# Windows VPS consoles may default to CP1252, which cannot encode the emoji
# used in operational messages. Reconfigure streams so logging never crashes
# the event loop while trying to display a diagnostic.
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="backslashreplace")
    except (AttributeError, ValueError):
        pass

structlog.configure(
    processors=[
        structlog.stdlib.add_log_level,
        structlog.processors.TimeStamper(fmt="iso"),
        structlog.dev.ConsoleRenderer(),
    ],
)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
logger = logging.getLogger(__name__)


async def create_executor(settings: TradeSettings, *, connect: bool = False) -> object:
    """Create the MT5 adapter without making Telegram depend on broker startup.

    Connection is normally deferred to the scheduler background task. The
    optional ``connect`` flag is retained for explicit diagnostic callers.
    """
    if not MT5_AVAILABLE:
        logger.error("❌ MetaTrader5 package not available. Bot cannot run in demo or live mode.")
        return None

    mode = settings.trading_mode
    creds = get_mt5_credentials(mode)
    
    if not creds["login"] or not creds["password"] or not creds["server"]:
        logger.error(f"❌ MT5 {mode.upper()} credentials not set in .env.")
        return None

    executor = MT5Executor(
        login=creds["login"],
        password=creds["password"],
        server=creds["server"],
        path=creds["path"],
    )
    
    if not connect:
        logger.info("MT5 executor created; connection deferred until Telegram control plane is online")
        return executor

    connected = await executor.connect()
    if not connected:
        logger.error(f"❌ Failed to connect to MT5 {mode.upper()} account {creds['login']}.")
        return executor # Return even if not connected so bot stays alive

    logger.info(f"✅ MT5 executor connected to {mode.upper()} account")
    return executor


async def main():
    """Initialize and start the bot."""
    # Load token
    token = get_telegram_token()
    if not token:
        logger.error("TELEGRAM_BOT_TOKEN not set. Copy .env.example to .env and configure.")
        sys.exit(1)

    admin_ids = get_admin_ids()
    if not admin_ids:
        logger.warning("TELEGRAM_ADMIN_IDS not set. No one can control the bot!")

    # Init database
    db_path = "smc_bot.db"
    await db.init_db(db_path)

    # Load or create settings
    settings = await db.load_settings()
    logger.info(f"Settings loaded: mode={settings.trading_mode}, auto_trade={settings.auto_trade}")

    # Create the adapter only. Broker connection is deliberately deferred until
    # Telegram polling is online so MT5 cannot make the control plane unresponsive.
    executor = await create_executor(settings, connect=False)

    # Create risk manager
    risk_manager = RiskManager(settings)

    # Create Telegram app
    from telegram.ext import Application
    app = Application.builder().token(token).build()

    # Create scheduler (bot_app set after init)
    scheduler = MarketScheduler(
        settings=settings,
        executor=executor,
        risk_manager=risk_manager,
        bot_app=app,
        admin_chat_id=admin_ids[0] if admin_ids else None,
        db_path=db_path,
    )

    # One command bus is shared by every future communication adapter. The
    # existing Telegram handlers remain compatible while Slack control is
    # enabled only when its explicit credentials and allow-list are present.
    control_service = SharedControlService(settings, scheduler, db_path=db_path)
    command_bus = build_command_bus(control_service)
    slack_control = SlackSocketControl(command_bus)

    # Create handlers
    handlers = BotHandlers(
        settings=settings,
        executor=executor,
        risk_manager=risk_manager,
        scheduler=scheduler,
        command_bus=command_bus,
    )
    handlers.setup(app)

    # Set bot commands menu
    from telegram import BotCommand
    commands = [
        BotCommand("start", "Open the Deriv research dashboard"),
        BotCommand("dashboard", "Current system and DEMO status"),
        BotCommand("account", "Fresh MT5 account overview"),
        BotCommand("positions", "Current MT5 positions"),
        BotCommand("position", "One MT5 position by ticket"),
        BotCommand("orders", "Current MT5 pending orders"),
        BotCommand("history", "MT5 closed-deal history: 1, 7, or 30 days"),
        BotCommand("exposure", "Current MT5 exposure"),
        BotCommand("health", "MT5 connection and account synchronization"),
        BotCommand("capital_test", "DEMO capital reduction / capital-test status"),
        BotCommand("capital_target", "Set DEMO reduction target and tolerance"),
        BotCommand("capital_start", "Request deliberate DEMO drawdown confirmation"),
        BotCommand("capital_pause", "Pause capital reduction"),
        BotCommand("capital_resume", "Resume paused capital reduction"),
        BotCommand("capital_cancel", "Cancel capital reduction"),
        BotCommand("capital_status", "Current capital reduction progress"),
        BotCommand("capital_activity", "Isolated capital reduction activity"),
        BotCommand("capital_enter_test", "Report completed DEMO capital-test transition"),
        BotCommand("demo_session", "Broker-verified reset-separated DEMO session report"),
        BotCommand("demo_auto_resume", "Set verified DEMO reset auto-resume on or off"),
        BotCommand("markets", "Broker-verified Deriv universe"),
        BotCommand("brokercheck", "Read-only MT5 price, volume, contract, and margin audit"),
        BotCommand("sizingtest", "Read-only full broker lot-size calculation for latest rejected setup"),
        BotCommand("engine", "Actual scheduler, scanner, analysis, execution, and task diagnostics"),
        BotCommand("learning", "Measured learning status"),
        BotCommand("learned", "Plain-language objective and strategy evidence"),
        BotCommand("knowledge", "Expert methodology hypotheses and evidence journal"),
        BotCommand("opportunities", "Current ranked strategy opportunity board"),
        BotCommand("experiments", "Policy experiment lifecycle"),
        BotCommand("champion", "Current validated champion policy"),
        BotCommand("challengers", "Forward-DEMO challenger policies"),
        BotCommand("research", "Falsifiable hypotheses and research queue"),
        BotCommand("performance", "DEMO/LIVE-separated results"),
        BotCommand("scorebuckets", "Setup score performance by bucket"),
        BotCommand("settings", "Autonomy, alerts, and DEMO/LIVE control"),
        BotCommand("backtest", "Causal policy backtest"),
        BotCommand("activity", "Chart-study notification mode"),
        BotCommand("emergency", "Pause execution; optionally close positions"),
        BotCommand("help", "Operational command guide"),
    ]
    # Start Telegram first. MT5 connection, symbol discovery, account
    # reconciliation, and scanning are broker-subsystem work and must never
    # prevent the control plane from becoming responsive.
    await app.initialize()
    await app.start()
    await app.updater.start_polling(allowed_updates=["message", "callback_query"])
    await slack_control.start()
    try:
        await app.bot.set_my_commands(commands)
    except Exception:
        logger.exception("Could not publish Telegram command menu; control plane remains online")

    await scheduler.notification_manager.publish(NotificationEvent(
        event_type="bot_started",
        message="\n".join([
            "🤖 **SMC Trading Bot Started**",
            "",
            f"Mode: `{settings.trading_mode.upper()}`",
            f"Auto-Trade: `{'ON' if settings.auto_trade else 'OFF'}`",
            "Telegram control plane: `ONLINE`",
            "MT5 broker subsystem: `STARTING`",
            "",
            "Use `/engine` or `/health` for live subsystem status.",
        ]),
        severity=EventSeverity.IMPORTANT,
        channels=(DeliveryChannel.TELEGRAM, DeliveryChannel.SLACK),
        dedupe_key="bot_started",
    ))

    logger.info("🚀 Telegram control plane is online; broker subsystem startup is running in background")
    logger.info(f"Admin IDs: {admin_ids}")
    logger.info(f"Mode: {settings.trading_mode}")

    async def _start_broker_subsystem() -> None:
        """Start broker discovery without taking Telegram down if it fails."""
        try:
            await scheduler.start(interval_seconds=60)
            logger.info("Broker subsystem startup completed")
            connected = bool(executor and await asyncio.wait_for(executor.is_connected(), timeout=10.0))
            status = "CONNECTED" if connected else "DISCONNECTED"
            await scheduler.notification_manager.publish(NotificationEvent(
                event_type="broker_status",
                message=f"🩺 **BROKER SUBSYSTEM READY**\n\nMT5: `{status}`\nMarket engine: `{'ARMED' if scheduler._running else 'NOT READY'}`\nUse `/engine` for details.",
                severity=EventSeverity.IMPORTANT,
                channels=(DeliveryChannel.TELEGRAM, DeliveryChannel.SLACK),
                dedupe_key=f"broker_ready:{status}",
            ))
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Broker subsystem startup failed; Telegram control plane remains online")
            await scheduler.notification_manager.publish(NotificationEvent(
                event_type="critical_broker_failure",
                message="⚠️ **BROKER SUBSYSTEM UNAVAILABLE**\n\nTelegram remains online. MT5-dependent trading is disabled until broker health recovers.",
                severity=EventSeverity.CRITICAL,
                channels=(DeliveryChannel.TELEGRAM, DeliveryChannel.SLACK),
                dedupe_key="broker_subsystem_unavailable",
            ))

    broker_startup_task = asyncio.create_task(_start_broker_subsystem(), name="broker_subsystem_startup")

    # Keep running until interrupted.
    stop_event = asyncio.Event()
    try:
        await stop_event.wait()
    except (KeyboardInterrupt, SystemExit):
        pass
    finally:
        if not broker_startup_task.done():
            broker_startup_task.cancel()
            await asyncio.gather(broker_startup_task, return_exceptions=True)
        await slack_control.stop()
        await app.updater.stop()
        await app.stop()
        await app.shutdown()
        if executor and hasattr(executor, "disconnect"):
            try:
                await executor.disconnect()
            except Exception:
                logger.exception("Failed to disconnect MT5 executor during shutdown")


if __name__ == "__main__":
    asyncio.run(main())
