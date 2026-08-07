"""
SQLite storage layer for settings persistence, trade history, and daily stats.
Uses aiosqlite for async operations.
"""

import aiosqlite
import json
from datetime import datetime, date
from typing import Optional
from config import TradeSettings

DB_PATH = "smc_bot.db"


async def init_db(db_path: str = DB_PATH) -> None:
    async with aiosqlite.connect(db_path) as db:
        await db.execute("""
            CREATE TABLE IF NOT EXISTS settings (
                id INTEGER PRIMARY KEY DEFAULT 1,
                data TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS trades (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp TEXT NOT NULL,
                symbol TEXT NOT NULL,
                direction TEXT NOT NULL,
                entry_price REAL,
                sl_price REAL,
                tp_price REAL,
                lot_size REAL,
                score REAL,
                rr_ratio REAL,
                status TEXT DEFAULT 'open',
                pnl REAL DEFAULT 0,
                executor TEXT DEFAULT 'paper',
                raw_signal TEXT
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS daily_stats (
                date TEXT PRIMARY KEY,
                trades_opened INTEGER DEFAULT 0,
                trades_closed INTEGER DEFAULT 0,
                pnl REAL DEFAULT 0,
                max_drawdown REAL DEFAULT 0
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS symbol_cooldowns (
                symbol TEXT PRIMARY KEY,
                last_trade_time TEXT NOT NULL
            )
        """)
        await db.commit()


async def load_settings(db_path: str = DB_PATH) -> TradeSettings:
    async with aiosqlite.connect(db_path) as db:
        cursor = await db.execute("SELECT data FROM settings WHERE id = 1")
        row = await cursor.fetchone()
        if row:
            return TradeSettings.from_dict(json.loads(row[0]))
    return TradeSettings.defaults()


async def save_settings(settings: TradeSettings, db_path: str = DB_PATH) -> None:
    async with aiosqlite.connect(db_path) as db:
        data = json.dumps(settings.to_dict())
        now = datetime.utcnow().isoformat()
        await db.execute(
            "INSERT OR REPLACE INTO settings (id, data, updated_at) VALUES (1, ?, ?)",
            (data, now)
        )
        await db.commit()


async def record_trade(
    symbol: str, direction: str, entry_price: float, sl_price: float,
    tp_price: float, lot_size: float, score: float, rr_ratio: float,
    executor: str, raw_signal: str, db_path: str = DB_PATH
) -> int:
    async with aiosqlite.connect(db_path) as db:
        now = datetime.utcnow().isoformat()
        cursor = await db.execute(
            """INSERT INTO trades (timestamp, symbol, direction, entry_price, sl_price, tp_price,
               lot_size, score, rr_ratio, executor, raw_signal)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (now, symbol, direction, entry_price, sl_price, tp_price,
             lot_size, score, rr_ratio, executor, raw_signal)
        )
        await db.commit()
        return cursor.lastrowid


async def close_trade(trade_id: int, pnl: float, db_path: str = DB_PATH) -> None:
    async with aiosqlite.connect(db_path) as db:
        await db.execute(
            "UPDATE trades SET status = 'closed', pnl = ? WHERE id = ?",
            (pnl, trade_id)
        )
        await db.commit()


async def get_open_trades(db_path: str = DB_PATH) -> list[dict]:
    async with aiosqlite.connect(db_path) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute("SELECT * FROM trades WHERE status = 'open' ORDER BY timestamp DESC")
        rows = await cursor.fetchall()
        return [dict(r) for r in rows]


async def get_today_trade_count(db_path: str = DB_PATH) -> int:
    today = date.today().isoformat()
    async with aiosqlite.connect(db_path) as db:
        cursor = await db.execute(
            "SELECT COUNT(*) FROM trades WHERE timestamp >= ?",
            (today,)
        )
        row = await cursor.fetchone()
        return row[0] if row else 0


async def get_today_pnl(db_path: str = DB_PATH) -> float:
    today = date.today().isoformat()
    async with aiosqlite.connect(db_path) as db:
        cursor = await db.execute(
            "SELECT COALESCE(SUM(pnl), 0) FROM trades WHERE timestamp >= ? AND status = 'closed'",
            (today,)
        )
        row = await cursor.fetchone()
        return row[0] if row else 0.0


async def set_symbol_cooldown(symbol: str, db_path: str = DB_PATH) -> None:
    async with aiosqlite.connect(db_path) as db:
        now = datetime.utcnow().isoformat()
        await db.execute(
            "INSERT OR REPLACE INTO symbol_cooldowns (symbol, last_trade_time) VALUES (?, ?)",
            (symbol, now)
        )
        await db.commit()


async def is_symbol_in_cooldown(symbol: str, cooldown_minutes: int, db_path: str = DB_PATH) -> bool:
    if cooldown_minutes <= 0:
        return False
    async with aiosqlite.connect(db_path) as db:
        cursor = await db.execute(
            "SELECT last_trade_time FROM symbol_cooldowns WHERE symbol = ?",
            (symbol,)
        )
        row = await cursor.fetchone()
        if not row:
            return False
        last_time = datetime.fromisoformat(row[0])
        elapsed = (datetime.utcnow() - last_time).total_seconds() / 60
        return elapsed < cooldown_minutes


async def get_trade_history(limit: int = 20, db_path: str = DB_PATH) -> list[dict]:
    async with aiosqlite.connect(db_path) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            "SELECT * FROM trades ORDER BY timestamp DESC LIMIT ?", (limit,)
        )
        rows = await cursor.fetchall()
        return [dict(r) for r in rows]
