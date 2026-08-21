import asyncio
import aiosqlite

async def migrate():
    async with aiosqlite.connect("smc_bot.db") as db:
        await db.execute("""
            CREATE TABLE IF NOT EXISTS capital_reduction_sessions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                account_mode TEXT NOT NULL,
                broker_login TEXT NOT NULL,
                status TEXT NOT NULL,
                target_equity REAL NOT NULL,
                tolerance REAL NOT NULL,
                initial_equity REAL NOT NULL,
                initial_balance REAL NOT NULL,
                current_equity REAL,
                current_balance REAL,
                started_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                completed_at TEXT,
                paused_at TEXT,
                error_reason TEXT,
                capital_test_active INTEGER NOT NULL DEFAULT 0,
                metadata_json TEXT NOT NULL DEFAULT '{}'
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS capital_reduction_actions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id INTEGER NOT NULL,
                created_at TEXT NOT NULL,
                action TEXT NOT NULL,
                status TEXT NOT NULL,
                symbol TEXT,
                direction TEXT,
                volume REAL,
                entry_price REAL,
                ticket INTEGER,
                equity_before REAL,
                equity_after REAL,
                details_json TEXT NOT NULL DEFAULT '{}',
                FOREIGN KEY(session_id) REFERENCES capital_reduction_sessions(id)
            )
        """)
        await db.commit()
    print("Capital reduction tables migration completed successfully.")

if __name__ == "__main__":
    asyncio.run(migrate())
