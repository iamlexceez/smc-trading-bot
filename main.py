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
from bot.keyboards import main_menu
from executors.paper import PaperExecutor
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
    """Create the appropriate executor based on settings."""
    if settings.trading_mode == "live" and MT5_AVAILABLE:
        creds = get_mt5_credentials()
        if creds["login"] and creds["password"] and creds["server"]:
            executor = MT5Executor(
                login=creds["login"],
                password=creds["password"],
                server=creds["server"],
                path=creds["path"],
            )
            connected = await executor.connect()
            if connected:
                logger.info("✅ MT5 executor connected")
                return executor
            else:
                logger.warning("⚠️ MT5 connection failed, falling back to paper mode")
        else:
            logger.warning("⚠️ MT5 credentials not set, falling back to paper mode")

    # Default: paper mode
    executor = PaperExecutor(starting_balance=10000.0)
    await executor.connect()
    logger.info("✅ Paper executor initialized ($10,000 balance)")
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

    # Create executor
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

    # Start scheduler
    await scheduler.start(interval_seconds=300)  # 5 minute scan interval

    # Notify admin
    if admin_ids:
        for admin_id in admin_ids:
            try:
                await app.bot.send_message(
                    admin_id,
                    f"🤖 **SMC Trading Bot Started**\n\n"
                    f"Mode: `{settings.trading_mode}`\n"
                    f"Auto-Trade: {'✅ ON' if settings.auto_trade else '❌ OFF'}\n"
                    f"Paper balance: $10,000\n\n"
                    f"Use /help to see all commands."
                )
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
