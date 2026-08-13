"""
SQLite storage layer for settings persistence, trade history, and daily stats.
Uses aiosqlite for async operations.
"""

import os
import aiosqlite
import json
from datetime import datetime, date, timedelta
from typing import Optional
from config import TradeSettings

DB_PATH = os.getenv("DB_PATH", "smc_bot.db")


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
        await db.execute("""
            CREATE TABLE IF NOT EXISTS trade_logs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                trade_id INTEGER,
                ticket INTEGER,
                timestamp TEXT NOT NULL,
                action TEXT NOT NULL,
                details TEXT,
                FOREIGN KEY(trade_id) REFERENCES trades(id)
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS trade_baskets (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                symbol TEXT NOT NULL,
                direction TEXT NOT NULL,
                entry_price REAL NOT NULL,
                initial_stop REAL NOT NULL,
                initial_target REAL NOT NULL,
                max_risk REAL NOT NULL,
                reserved_risk REAL NOT NULL,
                state TEXT NOT NULL DEFAULT 'initial',
                status TEXT NOT NULL DEFAULT 'open',
                planned_layers TEXT NOT NULL,
                metadata TEXT NOT NULL DEFAULT '{}'
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS trade_layers (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                basket_id INTEGER NOT NULL,
                ticket INTEGER,
                layer_number INTEGER NOT NULL,
                planned_volume REAL NOT NULL,
                executed_volume REAL NOT NULL DEFAULT 0,
                entry_price REAL,
                stop_loss REAL,
                take_profit REAL,
                status TEXT NOT NULL DEFAULT 'planned',
                trigger_reason TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                FOREIGN KEY(basket_id) REFERENCES trade_baskets(id)
            )
        """)
        await db.execute("CREATE INDEX IF NOT EXISTS idx_trade_layers_ticket ON trade_layers(ticket)")
        await db.execute("CREATE INDEX IF NOT EXISTS idx_trade_baskets_status ON trade_baskets(status)")
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


async def get_recent_trades(days: int = 7, db_path: str = DB_PATH) -> list[dict]:
    """Fetch trades from the last X days."""
    since = (datetime.utcnow() - timedelta(days=days)).isoformat()
    async with aiosqlite.connect(db_path) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            "SELECT * FROM trades WHERE timestamp >= ?", (since,)
        )
        rows = await cursor.fetchall()
        return [dict(r) for r in rows]


async def log_trade_action(ticket: int, action: str, details: str = "", trade_id: int = None, db_path: str = DB_PATH) -> None:
    """Record a management action for a specific trade."""
    async with aiosqlite.connect(db_path) as db:
        now = datetime.utcnow().isoformat()
        await db.execute(
            "INSERT INTO trade_logs (trade_id, ticket, timestamp, action, details) VALUES (?, ?, ?, ?, ?)",
            (trade_id, ticket, now, action, details)
        )
        await db.commit()


async def get_trade_logs(ticket: int = None, trade_id: int = None, db_path: str = DB_PATH) -> list[dict]:
    """Retrieve all logs for a specific trade ticket or internal ID."""
    async with aiosqlite.connect(db_path) as db:
        db.row_factory = aiosqlite.Row
        if ticket:
            cursor = await db.execute("SELECT * FROM trade_logs WHERE ticket = ? ORDER BY timestamp ASC", (ticket,))
        elif trade_id:
            cursor = await db.execute("SELECT * FROM trade_logs WHERE trade_id = ? ORDER BY timestamp ASC", (trade_id,))
        else:
            return []
        rows = await cursor.fetchall()
        return [dict(r) for r in rows]


async def create_trade_basket(
    *,
    symbol: str,
    direction: str,
    entry_price: float,
    initial_stop: float,
    initial_target: float,
    max_risk: float,
    reserved_risk: float,
    planned_layers: list[dict],
    metadata: Optional[dict] = None,
    db_path: str = DB_PATH,
) -> int:
    """Persist one setup-level risk budget and its future layer plan."""
    now = datetime.utcnow().isoformat()
    async with aiosqlite.connect(db_path) as conn:
        cursor = await conn.execute(
            """INSERT INTO trade_baskets
               (created_at, updated_at, symbol, direction, entry_price, initial_stop,
                initial_target, max_risk, reserved_risk, planned_layers, metadata)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                now,
                now,
                symbol,
                direction,
                entry_price,
                initial_stop,
                initial_target,
                max_risk,
                reserved_risk,
                json.dumps(planned_layers),
                json.dumps(metadata or {}),
            ),
        )
        await conn.commit()
        return cursor.lastrowid


async def record_trade_layer(
    *,
    basket_id: int,
    layer_number: int,
    planned_volume: float,
    executed_volume: float = 0.0,
    ticket: Optional[int] = None,
    entry_price: Optional[float] = None,
    stop_loss: Optional[float] = None,
    take_profit: Optional[float] = None,
    status: str = "planned",
    trigger_reason: str = "",
    db_path: str = DB_PATH,
) -> int:
    """Record a planned or executed layer without treating it as separate risk."""
    now = datetime.utcnow().isoformat()
    async with aiosqlite.connect(db_path) as conn:
        cursor = await conn.execute(
            """INSERT INTO trade_layers
               (basket_id, ticket, layer_number, planned_volume, executed_volume,
                entry_price, stop_loss, take_profit, status, trigger_reason,
                created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                basket_id,
                ticket,
                layer_number,
                planned_volume,
                executed_volume,
                entry_price,
                stop_loss,
                take_profit,
                status,
                trigger_reason,
                now,
                now,
            ),
        )
        await conn.commit()
        return cursor.lastrowid


async def get_basket_for_ticket(ticket: int, db_path: str = DB_PATH) -> Optional[dict]:
    """Return basket state and the specific layer associated with a live ticket."""
    async with aiosqlite.connect(db_path) as conn:
        conn.row_factory = aiosqlite.Row
        cursor = await conn.execute(
            """SELECT b.*, l.id AS layer_id, l.ticket, l.layer_number, l.planned_volume,
                      l.executed_volume, l.entry_price AS layer_entry_price,
                      l.stop_loss AS layer_stop_loss, l.take_profit AS layer_take_profit,
                      l.status AS layer_status, l.trigger_reason
               FROM trade_layers l
               JOIN trade_baskets b ON b.id = l.basket_id
               WHERE l.ticket = ? AND b.status = 'open'
               ORDER BY l.id DESC LIMIT 1""",
            (ticket,),
        )
        row = await cursor.fetchone()
        if not row:
            return None
        result = dict(row)
        result["planned_layers"] = json.loads(result["planned_layers"])
        result["metadata"] = json.loads(result["metadata"] or "{}")
        return result


async def get_open_baskets(db_path: str = DB_PATH) -> list[dict]:
    """Return all open baskets with their planned layer metadata."""
    async with aiosqlite.connect(db_path) as conn:
        conn.row_factory = aiosqlite.Row
        cursor = await conn.execute("SELECT * FROM trade_baskets WHERE status = 'open' ORDER BY created_at ASC")
        rows = await cursor.fetchall()
        results = []
        for row in rows:
            item = dict(row)
            item["planned_layers"] = json.loads(item["planned_layers"])
            item["metadata"] = json.loads(item["metadata"] or "{}")
            results.append(item)
        return results


async def update_basket_state(
    basket_id: int,
    *,
    state: Optional[str] = None,
    status: Optional[str] = None,
    metadata: Optional[dict] = None,
    db_path: str = DB_PATH,
) -> None:
    """Update management state with an atomic timestamp refresh."""
    assignments = ["updated_at = ?"]
    values: list = [datetime.utcnow().isoformat()]
    if state is not None:
        assignments.append("state = ?")
        values.append(state)
    if status is not None:
        assignments.append("status = ?")
        values.append(status)
    if metadata is not None:
        assignments.append("metadata = ?")
        values.append(json.dumps(metadata))
    values.append(basket_id)
    async with aiosqlite.connect(db_path) as conn:
        await conn.execute(f"UPDATE trade_baskets SET {', '.join(assignments)} WHERE id = ?", values)
        await conn.commit()


async def update_trade_layer(
    layer_id: int,
    *,
    status: Optional[str] = None,
    ticket: Optional[int] = None,
    executed_volume: Optional[float] = None,
    stop_loss: Optional[float] = None,
    take_profit: Optional[float] = None,
    trigger_reason: Optional[str] = None,
    db_path: str = DB_PATH,
) -> None:
    """Persist the latest layer execution or management state."""
    assignments = ["updated_at = ?"]
    values: list = [datetime.utcnow().isoformat()]
    for column, value in (
        ("status", status),
        ("ticket", ticket),
        ("executed_volume", executed_volume),
        ("stop_loss", stop_loss),
        ("take_profit", take_profit),
        ("trigger_reason", trigger_reason),
    ):
        if value is not None:
            assignments.append(f"{column} = ?")
            values.append(value)
    values.append(layer_id)
    async with aiosqlite.connect(db_path) as conn:
        await conn.execute(f"UPDATE trade_layers SET {', '.join(assignments)} WHERE id = ?", values)
        await conn.commit()


async def basket_has_action(basket_id: int, action: str, db_path: str = DB_PATH) -> bool:
    """Check whether an idempotent management action has already been taken."""
    marker = f'"basket_id": {basket_id}'
    async with aiosqlite.connect(db_path) as conn:
        cursor = await conn.execute(
            "SELECT 1 FROM trade_logs WHERE action = ? AND details LIKE ? LIMIT 1",
            (action, f"%{marker}%"),
        )
        return await cursor.fetchone() is not None


async def log_basket_action(
    *,
    basket_id: int,
    ticket: int,
    action: str,
    details: dict,
    trade_id: Optional[int] = None,
    db_path: str = DB_PATH,
) -> None:
    """Store structured management telemetry alongside the human trade log."""
    payload = {"basket_id": basket_id, **details}
    await log_trade_action(ticket, action, json.dumps(payload, sort_keys=True), trade_id=trade_id, db_path=db_path)


async def get_basket_layers(basket_id: int, db_path: str = DB_PATH) -> list[dict]:
    """Return all layers belonging to a basket in planned order."""
    async with aiosqlite.connect(db_path) as conn:
        conn.row_factory = aiosqlite.Row
        cursor = await conn.execute(
            "SELECT * FROM trade_layers WHERE basket_id = ? ORDER BY layer_number ASC, id ASC",
            (basket_id,),
        )
        rows = await cursor.fetchall()
        return [dict(row) for row in rows]


async def close_basket_if_flat(basket_id: int, live_tickets: set[int], db_path: str = DB_PATH) -> bool:
    """Close basket metadata when none of its executed layer tickets remain live."""
    layers = await get_basket_layers(basket_id, db_path)
    executed_tickets = {int(layer["ticket"]) for layer in layers if layer.get("ticket")}
    if executed_tickets and executed_tickets.isdisjoint(live_tickets):
        await update_basket_state(basket_id, status="closed", db_path=db_path)
        return True
    return False


async def get_consecutive_losses(limit: int = 50, db_path: str = DB_PATH) -> int:
    """Return the current closed-trade loss streak, newest trade first."""
    async with aiosqlite.connect(db_path) as conn:
        cursor = await conn.execute(
            """SELECT pnl FROM trades
               WHERE status = 'closed'
               ORDER BY timestamp DESC LIMIT ?""",
            (limit,),
        )
        rows = await cursor.fetchall()
    streak = 0
    for (pnl,) in rows:
        if float(pnl or 0) < 0:
            streak += 1
        else:
            break
    return streak
