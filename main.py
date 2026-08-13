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

# ─── Logging ──────────────────────────────────────────────

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


async def create_executor(settings: TradeSettings) -> object:
    """Create the appropriate MT5 executor based on settings (demo or live)."""
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

    # Create executor (demo or live MT5)
    executor = await create_executor(settings)

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
    )

    # Create handlers
    handlers = BotHandlers(
        settings=settings,
        executor=executor,
        risk_manager=risk_manager,
        scheduler=scheduler,
    )
    handlers.setup(app)

    # Set bot commands menu
    from telegram import BotCommand
    commands = [
        BotCommand("start", "Open the Deriv system dashboard"),
        BotCommand("dashboard", "Current autonomous-system status"),
        BotCommand("markets", "Broker-verified Deriv universe"),
        BotCommand("positions", "Active broker positions and management"),
        BotCommand("learning", "Measured learning status and next objective"),
        BotCommand("performance", "DEMO/LIVE-separated results"),
        BotCommand("settings", "Mode and safety controls"),
        BotCommand("backtest", "Causal broker-history backtest"),
        BotCommand("safety", "Hard limits and circuit breakers"),
        BotCommand("model", "Champion model and governance"),
        BotCommand("activity", "Chart-study notification mode"),
        BotCommand("emergency", "Pause execution; optionally close positions"),
        BotCommand("help", "Operational command guide"),
    ]
    await app.bot.set_my_commands(commands)

    # Start scheduler
    await scheduler.start(interval_seconds=60)  # closed-candle universe check every minute; no heartbeat notifications

    # Notify admin
    if admin_ids:
        for admin_id in admin_ids:
            try:
                status_msg = f"🤖 **SMC Trading Bot Started**\n\n"
                status_msg += f"Mode: `{settings.trading_mode.upper()}`\n"
                status_msg += f"Auto-Trade: {'✅ ON' if settings.auto_trade else '❌ OFF'}\n\n"
                
                if executor and await executor.is_connected():
                    status_msg += "✅ **MT5 Connected**"
                else:
                    status_msg += "❌ **MT5 Connection Failed**\nCheck if MT5 terminal is open on VPS and credentials are correct."
                
                status_msg += f"\n\nUse /help to see all commands."
                
                await app.bot.send_message(admin_id, status_msg)
            except Exception as e:
                logger.error(f"Failed to notify admin {admin_id}: {e}")

    logger.info("🚀 SMC Trading Bot is running!")
    logger.info(f"Admin IDs: {admin_ids}")
    logger.info(f"Mode: {settings.trading_mode}")
    logger.info(f"Symbols: {settings.symbols}")

    # Start polling using async lifecycle (compatible with asyncio.run)
    await app.initialize()
    await app.start()
    await app.updater.start_polling(allowed_updates=["message", "callback_query"])

    # Explicitly start the first scan if not running
    if not scheduler._running:
        await scheduler.start()

    # Keep running until interrupted
    stop_event = asyncio.Event()
    try:
        await stop_event.wait()
    except (KeyboardInterrupt, SystemExit):
        pass
    finally:
        await app.updater.stop()
        await app.stop()
        await app.shutdown()


if __name__ == "__main__":
    asyncio.run(main())
