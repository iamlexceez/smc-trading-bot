"""
SQLite storage layer for settings persistence, trade history, and daily stats.
Uses aiosqlite for async operations.
"""

import os
import aiosqlite
import json
from datetime import datetime, date, timedelta
from typing import Any, Optional
from config import TradeSettings

DB_PATH = os.getenv("DB_PATH", "smc_bot.db")


async def _ensure_column(conn: aiosqlite.Connection, table: str, column: str, definition: str) -> None:
    """Apply additive SQLite schema migrations without destroying history."""
    cursor = await conn.execute(f"PRAGMA table_info({table})")
    existing = {row[1] for row in await cursor.fetchall()}
    if column not in existing:
        await conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")


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
                account_mode TEXT NOT NULL DEFAULT 'demo',
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
            CREATE TABLE IF NOT EXISTS daily_performance (
                date TEXT NOT NULL,
                account_mode TEXT NOT NULL,
                trades_opened INTEGER DEFAULT 0,
                trades_closed INTEGER DEFAULT 0,
                pnl REAL DEFAULT 0,
                max_drawdown REAL DEFAULT 0,
                PRIMARY KEY (date, account_mode)
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS setup_records (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                detected_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                account_mode TEXT NOT NULL,
                symbol TEXT NOT NULL,
                timeframe TEXT NOT NULL,
                direction TEXT,
                setup_type TEXT,
                status TEXT NOT NULL,
                rejection_reason TEXT,
                entry_price REAL,
                stop_loss REAL,
                take_profit REAL,
                rr_ratio REAL,
                quality_score REAL,
                validation_json TEXT NOT NULL DEFAULT '{}',
                features_json TEXT NOT NULL DEFAULT '{}',
                outcome_json TEXT NOT NULL DEFAULT '{}',
                trade_id INTEGER,
                FOREIGN KEY(trade_id) REFERENCES trades(id)
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS execution_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                created_at TEXT NOT NULL,
                account_mode TEXT NOT NULL,
                setup_id INTEGER,
                trade_id INTEGER,
                ticket INTEGER,
                symbol TEXT NOT NULL,
                requested_price REAL,
                executed_price REAL,
                slippage REAL,
                execution_delay_ms REAL,
                status TEXT NOT NULL,
                reason TEXT,
                details_json TEXT NOT NULL DEFAULT '{}',
                FOREIGN KEY(setup_id) REFERENCES setup_records(id),
                FOREIGN KEY(trade_id) REFERENCES trades(id)
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS symbol_profiles (
                account_mode TEXT NOT NULL,
                symbol TEXT NOT NULL,
                timeframe TEXT NOT NULL,
                metrics_json TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                PRIMARY KEY (account_mode, symbol, timeframe)
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS model_versions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                account_mode TEXT NOT NULL,
                version TEXT NOT NULL,
                role TEXT NOT NULL,
                status TEXT NOT NULL,
                previous_version TEXT,
                created_at TEXT NOT NULL,
                promoted_at TEXT,
                training_start TEXT,
                training_end TEXT,
                validation_start TEXT,
                validation_end TEXT,
                out_of_sample_start TEXT,
                out_of_sample_end TEXT,
                parameters_json TEXT NOT NULL,
                performance_json TEXT NOT NULL,
                reason TEXT NOT NULL,
                UNIQUE(account_mode, version)
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS optimization_runs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                account_mode TEXT NOT NULL,
                created_at TEXT NOT NULL,
                champion_version TEXT,
                challenger_version TEXT,
                decision TEXT NOT NULL,
                details_json TEXT NOT NULL
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS research_hypotheses (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                account_mode TEXT NOT NULL,
                hypothesis_key TEXT NOT NULL,
                statement TEXT NOT NULL,
                source TEXT NOT NULL,
                feature_name TEXT,
                candidate_values_json TEXT NOT NULL,
                evidence_json TEXT NOT NULL DEFAULT '{}',
                status TEXT NOT NULL DEFAULT 'open',
                created_at TEXT NOT NULL,
                archived_at TEXT,
                UNIQUE(account_mode, hypothesis_key, statement)
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS policy_experiments (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                account_mode TEXT NOT NULL,
                policy_fingerprint TEXT NOT NULL,
                policy_json TEXT NOT NULL,
                hypothesis_id INTEGER,
                status TEXT NOT NULL,
                created_at TEXT NOT NULL,
                forward_started_at TEXT,
                completed_at TEXT,
                evaluation_json TEXT NOT NULL DEFAULT '{}',
                reason TEXT NOT NULL,
                model_version TEXT,
                FOREIGN KEY(hypothesis_id) REFERENCES research_hypotheses(id),
                UNIQUE(account_mode, policy_fingerprint)
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS trading_objectives (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                account_mode TEXT NOT NULL,
                version INTEGER,
                status TEXT NOT NULL,
                raw_instruction TEXT NOT NULL,
                objective_json TEXT NOT NULL,
                account_snapshot_json TEXT NOT NULL DEFAULT '{}',
                broker_universe_json TEXT NOT NULL DEFAULT '[]',
                context_json TEXT NOT NULL DEFAULT '{}',
                created_at TEXT NOT NULL,
                confirmed_at TEXT,
                superseded_at TEXT,
                cancelled_at TEXT,
                is_paused INTEGER NOT NULL DEFAULT 0
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS demo_sessions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                broker_login TEXT NOT NULL,
                status TEXT NOT NULL,
                started_at TEXT NOT NULL,
                ended_at TEXT,
                start_balance REAL NOT NULL,
                start_equity REAL NOT NULL,
                end_balance REAL,
                end_equity REAL,
                max_equity REAL NOT NULL,
                min_equity REAL NOT NULL,
                max_drawdown_pct REAL NOT NULL DEFAULT 0,
                exhaustion_reason TEXT,
                reset_detected_at TEXT,
                strategy_trades INTEGER NOT NULL DEFAULT 0,
                wins INTEGER NOT NULL DEFAULT 0,
                losses INTEGER NOT NULL DEFAULT 0,
                profit_factor REAL,
                expectancy_r REAL,
                capital_reduction_activity INTEGER NOT NULL DEFAULT 0,
                capital_test_active INTEGER NOT NULL DEFAULT 0,
                policy_versions_json TEXT NOT NULL DEFAULT '[]'
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS objective_session_reviews (
                demo_session_id INTEGER PRIMARY KEY,
                objective_id INTEGER,
                outcome TEXT NOT NULL,
                terminal_state TEXT NOT NULL,
                created_at TEXT NOT NULL,
                completed_at TEXT,
                summary_json TEXT NOT NULL DEFAULT '{}',
                optimization_json TEXT NOT NULL DEFAULT '{}',
                FOREIGN KEY(demo_session_id) REFERENCES demo_sessions(id),
                FOREIGN KEY(objective_id) REFERENCES trading_objectives(id)
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS objective_phases (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                objective_id INTEGER NOT NULL,
                demo_session_id INTEGER,
                phase_number INTEGER NOT NULL,
                status TEXT NOT NULL DEFAULT 'planned',
                planned_start_equity REAL NOT NULL,
                starting_equity REAL,
                target_equity REAL NOT NULL,
                ending_equity REAL,
                started_at TEXT,
                completed_at TEXT,
                completion_reason TEXT,
                policy_snapshot_json TEXT NOT NULL DEFAULT '{}',
                instruments_json TEXT NOT NULL DEFAULT '[]',
                metrics_json TEXT NOT NULL DEFAULT '{}',
                UNIQUE(objective_id, phase_number),
                FOREIGN KEY(objective_id) REFERENCES trading_objectives(id),
                FOREIGN KEY(demo_session_id) REFERENCES demo_sessions(id)
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS account_state (
                account_mode TEXT PRIMARY KEY,
                broker_login TEXT,
                state TEXT NOT NULL,
                state_changed_at TEXT NOT NULL,
                last_checked_at TEXT NOT NULL,
                last_balance REAL,
                last_equity REAL,
                last_free_margin REAL,
                last_margin_level REAL,
                minimum_operating_capital REAL,
                active_demo_session_id INTEGER,
                exhaustion_reason TEXT,
                reset_previous_balance REAL,
                reset_previous_equity REAL,
                reset_detected_at TEXT,
                notification_key TEXT,
                metadata_json TEXT NOT NULL DEFAULT '{}'
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS account_state_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                account_mode TEXT NOT NULL,
                broker_login TEXT,
                demo_session_id INTEGER,
                event_type TEXT NOT NULL,
                state TEXT NOT NULL,
                created_at TEXT NOT NULL,
                balance REAL,
                equity REAL,
                free_margin REAL,
                margin_level REAL,
                minimum_operating_capital REAL,
                details_json TEXT NOT NULL DEFAULT '{}'
            )
        """)
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
                account_mode TEXT NOT NULL DEFAULT 'demo',
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
        await _ensure_column(db, "trades", "account_mode", "TEXT NOT NULL DEFAULT 'demo'")
        await _ensure_column(db, "trades", "ticket", "INTEGER")
        await _ensure_column(db, "trades", "setup_id", "INTEGER")
        await _ensure_column(db, "trades", "initial_risk", "REAL DEFAULT 0")
        await _ensure_column(db, "trades", "exit_price", "REAL")
        await _ensure_column(db, "trades", "exit_reason", "TEXT")
        await _ensure_column(db, "trades", "closed_at", "TEXT")
        await _ensure_column(db, "trades", "pnl_r", "REAL")
        await _ensure_column(db, "trades", "max_favorable_r", "REAL DEFAULT 0")
        await _ensure_column(db, "trades", "max_adverse_r", "REAL DEFAULT 0")
        await _ensure_column(db, "trades", "policy_version", "TEXT")
        await _ensure_column(db, "trades", "experiment_id", "INTEGER")
        await _ensure_column(db, "trades", "demo_session_id", "INTEGER")
        await _ensure_column(db, "trades", "objective_phase_id", "INTEGER")
        await _ensure_column(db, "setup_records", "objective_phase_id", "INTEGER")
        await _ensure_column(db, "trade_baskets", "objective_phase_id", "INTEGER")
        await _ensure_column(db, "setup_records", "policy_version", "TEXT")
        await _ensure_column(db, "setup_records", "experiment_id", "INTEGER")
        await _ensure_column(db, "trade_baskets", "policy_version", "TEXT")
        await _ensure_column(db, "trade_baskets", "experiment_id", "INTEGER")
        await _ensure_column(db, "trade_baskets", "account_mode", "TEXT NOT NULL DEFAULT 'demo'")
        await db.execute("CREATE INDEX IF NOT EXISTS idx_trades_mode_status ON trades(account_mode, status)")
        await db.execute("CREATE INDEX IF NOT EXISTS idx_trades_demo_session ON trades(demo_session_id, status)")
        await db.execute("CREATE INDEX IF NOT EXISTS idx_trades_objective_phase ON trades(objective_phase_id, status)")
        await db.execute("CREATE INDEX IF NOT EXISTS idx_setups_objective_phase ON setup_records(objective_phase_id, status)")
        await db.execute("CREATE INDEX IF NOT EXISTS idx_objective_phases_objective_status ON objective_phases(objective_id, status, phase_number)")
        await db.execute("CREATE INDEX IF NOT EXISTS idx_setups_mode_status ON setup_records(account_mode, status)")
        await db.execute("CREATE INDEX IF NOT EXISTS idx_execution_events_trade ON execution_events(trade_id)")
        await db.execute("CREATE INDEX IF NOT EXISTS idx_model_versions_mode_role ON model_versions(account_mode, role, status)")
        await db.execute("CREATE INDEX IF NOT EXISTS idx_objectives_mode_status ON trading_objectives(account_mode, status, id)")
        await db.execute("CREATE INDEX IF NOT EXISTS idx_hypotheses_mode_status ON research_hypotheses(account_mode, status)")
        await db.execute("CREATE INDEX IF NOT EXISTS idx_experiments_mode_status ON policy_experiments(account_mode, status)")
        await db.execute("CREATE INDEX IF NOT EXISTS idx_demo_sessions_login_status ON demo_sessions(broker_login, status)")
        await db.execute("CREATE INDEX IF NOT EXISTS idx_account_state_events_mode_time ON account_state_events(account_mode, created_at)")
        await db.execute("CREATE INDEX IF NOT EXISTS idx_capital_reduction_mode_status ON capital_reduction_sessions(account_mode, status)")
        await db.execute("CREATE INDEX IF NOT EXISTS idx_capital_reduction_actions_session ON capital_reduction_actions(session_id, created_at)")
        await db.execute("CREATE INDEX IF NOT EXISTS idx_trade_layers_ticket ON trade_layers(ticket)")
        await db.execute("CREATE INDEX IF NOT EXISTS idx_trade_baskets_status ON trade_baskets(status)")
        await db.execute("CREATE INDEX IF NOT EXISTS idx_trade_baskets_mode_status ON trade_baskets(account_mode, status)")
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
    executor: str, raw_signal: str, account_mode: str = "demo", ticket: Optional[int] = None,
    setup_id: Optional[int] = None, initial_risk: float = 0.0,
    policy_version: Optional[str] = None, experiment_id: Optional[int] = None,
    demo_session_id: Optional[int] = None, objective_phase_id: Optional[int] = None, db_path: str = DB_PATH
) -> int:
    async with aiosqlite.connect(db_path) as db:
        now = datetime.utcnow().isoformat()
        cursor = await db.execute(
            """INSERT INTO trades (timestamp, symbol, direction, entry_price, sl_price, tp_price,
               lot_size, score, rr_ratio, executor, account_mode, ticket, setup_id, initial_risk,
               raw_signal, policy_version, experiment_id, demo_session_id, objective_phase_id)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (now, symbol, direction, entry_price, sl_price, tp_price,
             lot_size, score, rr_ratio, executor, account_mode, ticket, setup_id, initial_risk,
             raw_signal, policy_version, experiment_id, demo_session_id, objective_phase_id)
        )
        await db.commit()
        return cursor.lastrowid


async def close_trade(
    trade_id: int,
    pnl: float,
    db_path: str = DB_PATH,
    *,
    exit_price: Optional[float] = None,
    exit_reason: Optional[str] = None,
    pnl_r: Optional[float] = None,
    max_favorable_r: Optional[float] = None,
    max_adverse_r: Optional[float] = None,
) -> None:
    """Close one recorded trade while retaining its learning outcome."""
    assignments = ["status = 'closed'", "pnl = ?", "closed_at = ?"]
    values: list = [pnl, datetime.utcnow().isoformat()]
    for column, value in (
        ("exit_price", exit_price),
        ("exit_reason", exit_reason),
        ("pnl_r", pnl_r),
        ("max_favorable_r", max_favorable_r),
        ("max_adverse_r", max_adverse_r),
    ):
        if value is not None:
            assignments.append(f"{column} = ?")
            values.append(value)
    values.append(trade_id)
    async with aiosqlite.connect(db_path) as db:
        await db.execute(f"UPDATE trades SET {', '.join(assignments)} WHERE id = ?", values)
        await db.commit()


async def update_trade_excursions(
    trade_id: int,
    *,
    current_r: float,
    db_path: str = DB_PATH,
) -> None:
    """Update observed MFE/MAE using only the current closed-candle price."""
    async with aiosqlite.connect(db_path) as conn:
        cursor = await conn.execute(
            "SELECT max_favorable_r, max_adverse_r FROM trades WHERE id = ?", (trade_id,)
        )
        row = await cursor.fetchone()
        if not row:
            return
        mfe = max(float(row[0] or 0.0), float(current_r))
        mae = min(float(row[1] or 0.0), float(current_r))
        await conn.execute(
            "UPDATE trades SET max_favorable_r = ?, max_adverse_r = ? WHERE id = ?",
            (mfe, mae, trade_id),
        )
        await conn.commit()


async def record_setup(
    *,
    account_mode: str,
    symbol: str,
    timeframe: str,
    direction: str,
    setup_type: str,
    status: str,
    rejection_reason: str = "",
    entry_price: Optional[float] = None,
    stop_loss: Optional[float] = None,
    take_profit: Optional[float] = None,
    rr_ratio: Optional[float] = None,
    quality_score: Optional[float] = None,
    validation: Optional[dict] = None,
    features: Optional[dict] = None,
    policy_version: Optional[str] = None,
    experiment_id: Optional[int] = None,
    objective_phase_id: Optional[int] = None,
    db_path: str = DB_PATH,
) -> int:
    """Store every qualifying, rejected, and executed setup hypothesis."""
    now = datetime.utcnow().isoformat()
    async with aiosqlite.connect(db_path) as conn:
        cursor = await conn.execute(
            """INSERT INTO setup_records
               (detected_at, updated_at, account_mode, symbol, timeframe, direction, setup_type,
                status, rejection_reason, entry_price, stop_loss, take_profit, rr_ratio,
                quality_score, validation_json, features_json, policy_version, experiment_id, objective_phase_id)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                now, now, account_mode, symbol, timeframe, direction, setup_type, status,
                rejection_reason, entry_price, stop_loss, take_profit, rr_ratio, quality_score,
                json.dumps(validation or {}, sort_keys=True), json.dumps(features or {}, sort_keys=True),
                policy_version, experiment_id, objective_phase_id,
            ),
        )
        await conn.commit()
        return cursor.lastrowid


async def update_setup_record(
    setup_id: int,
    *,
    status: Optional[str] = None,
    rejection_reason: Optional[str] = None,
    trade_id: Optional[int] = None,
    outcome: Optional[dict] = None,
    db_path: str = DB_PATH,
) -> None:
    """Update a setup's execution link or counterfactual/realized outcome."""
    assignments = ["updated_at = ?"]
    values: list = [datetime.utcnow().isoformat()]
    for column, value in (
        ("status", status),
        ("rejection_reason", rejection_reason),
        ("trade_id", trade_id),
        ("outcome_json", json.dumps(outcome, sort_keys=True) if outcome is not None else None),
    ):
        if value is not None:
            assignments.append(f"{column} = ?")
            values.append(value)
    values.append(setup_id)
    async with aiosqlite.connect(db_path) as conn:
        await conn.execute(f"UPDATE setup_records SET {', '.join(assignments)} WHERE id = ?", values)
        await conn.commit()


async def record_execution_event(
    *,
    account_mode: str,
    symbol: str,
    status: str,
    setup_id: Optional[int] = None,
    trade_id: Optional[int] = None,
    ticket: Optional[int] = None,
    requested_price: Optional[float] = None,
    executed_price: Optional[float] = None,
    execution_delay_ms: Optional[float] = None,
    reason: str = "",
    details: Optional[dict] = None,
    db_path: str = DB_PATH,
) -> int:
    """Record broker execution quality without treating failures as trades."""
    slippage = (executed_price - requested_price) if requested_price is not None and executed_price is not None else None
    async with aiosqlite.connect(db_path) as conn:
        cursor = await conn.execute(
            """INSERT INTO execution_events
               (created_at, account_mode, setup_id, trade_id, ticket, symbol, requested_price,
                executed_price, slippage, execution_delay_ms, status, reason, details_json)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                datetime.utcnow().isoformat(), account_mode, setup_id, trade_id, ticket, symbol,
                requested_price, executed_price, slippage, execution_delay_ms, status, reason,
                json.dumps(details or {}, sort_keys=True),
            ),
        )
        await conn.commit()
        return cursor.lastrowid


async def get_latest_sizing_rejection(
    *,
    account_mode: str,
    symbol: str,
    db_path: str = DB_PATH,
) -> Optional[dict]:
    """Return the latest persisted sizing-rejected setup plus its event evidence; read-only."""
    async with aiosqlite.connect(db_path) as conn:
        conn.row_factory = aiosqlite.Row
        cursor = await conn.execute(
            """SELECT e.*, s.direction, s.entry_price, s.stop_loss, s.take_profit, s.rr_ratio,
                      s.timeframe, s.policy_version, s.experiment_id, s.rejection_reason AS setup_rejection_reason
               FROM execution_events e
               LEFT JOIN setup_records s ON s.id = e.setup_id
               WHERE e.account_mode = ? AND e.symbol = ? AND e.status = 'sizing_rejected'
               ORDER BY e.created_at DESC, e.id DESC LIMIT 1""",
            (account_mode, symbol),
        )
        row = await cursor.fetchone()
    if row is None:
        return None
    result = dict(row)
    try:
        result["details"] = json.loads(result.pop("details_json") or "{}")
    except (TypeError, ValueError, json.JSONDecodeError):
        result["details"] = {}
    return result


async def upsert_symbol_profile(
    *,
    account_mode: str,
    symbol: str,
    timeframe: str,
    metrics: dict,
    db_path: str = DB_PATH,
) -> None:
    """Persist one broker-market behavioral profile without overwriting other modes."""
    async with aiosqlite.connect(db_path) as conn:
        await conn.execute(
            """INSERT INTO symbol_profiles (account_mode, symbol, timeframe, metrics_json, updated_at)
               VALUES (?, ?, ?, ?, ?)
               ON CONFLICT(account_mode, symbol, timeframe)
               DO UPDATE SET metrics_json = excluded.metrics_json, updated_at = excluded.updated_at""",
            (account_mode, symbol, timeframe, json.dumps(metrics, sort_keys=True), datetime.utcnow().isoformat()),
        )
        await conn.commit()


async def get_symbol_profile(
    symbol: str,
    timeframe: str,
    account_mode: str = "demo",
    db_path: str = DB_PATH,
) -> Optional[dict]:
    """Load a persisted behavioral profile for one symbol, timeframe, and mode."""
    async with aiosqlite.connect(db_path) as conn:
        cursor = await conn.execute(
            "SELECT metrics_json, updated_at FROM symbol_profiles WHERE account_mode = ? AND symbol = ? AND timeframe = ?",
            (account_mode, symbol, timeframe),
        )
        row = await cursor.fetchone()
    if not row:
        return None
    metrics = json.loads(row[0] or "{}")
    metrics["updated_at"] = row[1]
    return metrics


async def get_symbol_setup_metrics(
    symbol: str,
    timeframe: str,
    account_mode: str = "demo",
    db_path: str = DB_PATH,
) -> dict:
    """Aggregate only completed real trades for one learned market profile."""
    async with aiosqlite.connect(db_path) as conn:
        cursor = await conn.execute(
            """SELECT validation_json, outcome_json FROM setup_records
               WHERE account_mode = ? AND symbol = ? AND timeframe = ? AND status = 'closed' AND trade_id IS NOT NULL""",
            (account_mode, symbol, timeframe),
        )
        rows = await cursor.fetchall()

    buckets: dict[str, list[float]] = {"all": [], "order_block": [], "fvg": []}
    for validation_raw, outcome_raw in rows:
        validation = json.loads(validation_raw or "{}")
        outcome = json.loads(outcome_raw or "{}")
        pnl_r = outcome.get("pnl_r")
        if pnl_r is None:
            continue
        value = float(pnl_r)
        buckets["all"].append(value)
        zone = validation.get("zone") or {}
        source = zone.get("source")
        if source in buckets:
            buckets[source].append(value)

    def summarize(values: list[float]) -> dict:
        return {
            "sample_size": len(values),
            "win_rate": (sum(1 for value in values if value > 0) / len(values) * 100) if values else 0.0,
            "expectancy_r": (sum(values) / len(values)) if values else 0.0,
        }

    return {name: summarize(values) for name, values in buckets.items()}


async def create_model_version(
    *,
    account_mode: str,
    version: str,
    role: str,
    status: str,
    parameters: dict,
    performance: dict,
    reason: str,
    previous_version: Optional[str] = None,
    windows: Optional[dict] = None,
    promoted: bool = False,
    db_path: str = DB_PATH,
) -> None:
    """Persist an immutable model candidate or champion with its evidence."""
    windows = windows or {}
    now = datetime.utcnow().isoformat()
    async with aiosqlite.connect(db_path) as conn:
        await conn.execute(
            """INSERT INTO model_versions
               (account_mode, version, role, status, previous_version, created_at, promoted_at,
                training_start, training_end, validation_start, validation_end, out_of_sample_start,
                out_of_sample_end, parameters_json, performance_json, reason)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                account_mode, version, role, status, previous_version, now, now if promoted else None,
                windows.get("training_start"), windows.get("training_end"),
                windows.get("validation_start"), windows.get("validation_end"),
                windows.get("out_of_sample_start"), windows.get("out_of_sample_end"),
                json.dumps(parameters, sort_keys=True), json.dumps(performance, sort_keys=True), reason,
            ),
        )
        await conn.commit()


async def get_model_version(version: str, account_mode: str = "demo", db_path: str = DB_PATH) -> Optional[dict]:
    """Load one versioned model and deserialize its evidence."""
    async with aiosqlite.connect(db_path) as conn:
        conn.row_factory = aiosqlite.Row
        cursor = await conn.execute(
            "SELECT * FROM model_versions WHERE account_mode = ? AND version = ?", (account_mode, version)
        )
        row = await cursor.fetchone()
    if not row:
        return None
    item = dict(row)
    item["parameters"] = json.loads(item.pop("parameters_json") or "{}")
    item["performance"] = json.loads(item.pop("performance_json") or "{}")
    return item


async def list_model_versions(account_mode: str = "demo", limit: int = 50, db_path: str = DB_PATH) -> list[dict]:
    """Return immutable model versions with their stored evaluation evidence."""
    async with aiosqlite.connect(db_path) as conn:
        conn.row_factory = aiosqlite.Row
        cursor = await conn.execute(
            """SELECT * FROM model_versions WHERE account_mode = ?
               ORDER BY COALESCE(promoted_at, created_at) DESC, id DESC LIMIT ?""",
            (account_mode, max(1, int(limit))),
        )
        rows = [dict(row) for row in await cursor.fetchall()]
    for item in rows:
        item["parameters"] = json.loads(item.pop("parameters_json") or "{}")
        item["performance"] = json.loads(item.pop("performance_json") or "{}")
    return rows


async def get_active_model(account_mode: str = "demo", db_path: str = DB_PATH) -> Optional[dict]:
    """Return the active champion for exactly one account mode."""
    async with aiosqlite.connect(db_path) as conn:
        conn.row_factory = aiosqlite.Row
        cursor = await conn.execute(
            """SELECT * FROM model_versions
               WHERE account_mode = ? AND role = 'champion' AND status = 'active'
               ORDER BY promoted_at DESC, created_at DESC LIMIT 1""",
            (account_mode,),
        )
        row = await cursor.fetchone()
    if not row:
        return None
    item = dict(row)
    item["parameters"] = json.loads(item.pop("parameters_json") or "{}")
    item["performance"] = json.loads(item.pop("performance_json") or "{}")
    return item


async def activate_model_version(
    version: str,
    *,
    account_mode: str = "demo",
    previous_version: Optional[str] = None,
    db_path: str = DB_PATH,
) -> None:
    """Atomically replace the active champion while preserving version history."""
    now = datetime.utcnow().isoformat()
    async with aiosqlite.connect(db_path) as conn:
        await conn.execute(
            "UPDATE model_versions SET status = 'superseded' WHERE account_mode = ? AND role = 'champion' AND status = 'active'",
            (account_mode,),
        )
        await conn.execute(
            """UPDATE model_versions SET role = 'champion', status = 'active', previous_version = ?, promoted_at = ?
               WHERE account_mode = ? AND version = ?""",
            (previous_version, now, account_mode, version),
        )
        await conn.commit()


async def log_optimization_run(
    *,
    account_mode: str,
    decision: str,
    details: dict,
    champion_version: Optional[str] = None,
    challenger_version: Optional[str] = None,
    db_path: str = DB_PATH,
) -> None:
    """Store an explainable optimization decision, including no-change outcomes."""
    async with aiosqlite.connect(db_path) as conn:
        await conn.execute(
            """INSERT INTO optimization_runs
               (account_mode, created_at, champion_version, challenger_version, decision, details_json)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (account_mode, datetime.utcnow().isoformat(), champion_version, challenger_version, decision, json.dumps(details, sort_keys=True)),
        )
        await conn.commit()


async def get_recent_optimization_runs(account_mode: str = "demo", limit: int = 10, db_path: str = DB_PATH) -> list[dict]:
    """Return recent model decisions for Telegram reporting and audit."""
    async with aiosqlite.connect(db_path) as conn:
        conn.row_factory = aiosqlite.Row
        cursor = await conn.execute(
            "SELECT * FROM optimization_runs WHERE account_mode = ? ORDER BY created_at DESC LIMIT ?",
            (account_mode, limit),
        )
        rows = [dict(row) for row in await cursor.fetchall()]
    for row in rows:
        row["details"] = json.loads(row.pop("details_json") or "{}")
    return rows


async def upsert_research_hypothesis(
    *,
    account_mode: str,
    hypothesis_key: str,
    statement: str,
    source: str,
    feature_name: Optional[str],
    candidate_values: list | tuple,
    evidence: Optional[dict] = None,
    db_path: str = DB_PATH,
) -> int:
    """Persist a falsifiable research hypothesis without overwriting prior evidence."""
    now = datetime.utcnow().isoformat()
    async with aiosqlite.connect(db_path) as conn:
        await conn.execute(
            """INSERT INTO research_hypotheses
               (account_mode, hypothesis_key, statement, source, feature_name,
                candidate_values_json, evidence_json, status, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, 'open', ?)
               ON CONFLICT(account_mode, hypothesis_key, statement) DO UPDATE SET
                 evidence_json = excluded.evidence_json,
                 status = CASE WHEN research_hypotheses.status = 'archived' THEN 'archived' ELSE 'open' END""",
            (
                account_mode, hypothesis_key, statement, source, feature_name,
                json.dumps(list(candidate_values)), json.dumps(evidence or {}, sort_keys=True), now,
            ),
        )
        cursor = await conn.execute(
            """SELECT id FROM research_hypotheses
               WHERE account_mode = ? AND hypothesis_key = ? AND statement = ?""",
            (account_mode, hypothesis_key, statement),
        )
        row = await cursor.fetchone()
        await conn.commit()
    return int(row[0])


async def get_open_hypotheses(account_mode: str = "demo", db_path: str = DB_PATH) -> list[dict]:
    async with aiosqlite.connect(db_path) as conn:
        conn.row_factory = aiosqlite.Row
        cursor = await conn.execute(
            """SELECT * FROM research_hypotheses
               WHERE account_mode = ? AND status = 'open' ORDER BY created_at ASC""",
            (account_mode,),
        )
        rows = [dict(row) for row in await cursor.fetchall()]
    for row in rows:
        row["candidate_values"] = json.loads(row.pop("candidate_values_json") or "[]")
        row["evidence"] = json.loads(row.pop("evidence_json") or "{}")
    return rows


async def create_policy_experiment(
    *,
    account_mode: str,
    policy_fingerprint: str,
    policy: dict,
    reason: str,
    hypothesis_id: Optional[int] = None,
    status: str = "candidate",
    model_version: Optional[str] = None,
    db_path: str = DB_PATH,
) -> int:
    """Create an immutable policy experiment, returning its stable id."""
    now = datetime.utcnow().isoformat()
    async with aiosqlite.connect(db_path) as conn:
        await conn.execute(
            """INSERT INTO policy_experiments
               (account_mode, policy_fingerprint, policy_json, hypothesis_id, status,
                created_at, reason, model_version)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(account_mode, policy_fingerprint) DO NOTHING""",
            (account_mode, policy_fingerprint, json.dumps(policy, sort_keys=True), hypothesis_id, status, now, reason, model_version),
        )
        cursor = await conn.execute(
            """SELECT id FROM policy_experiments
               WHERE account_mode = ? AND policy_fingerprint = ?""",
            (account_mode, policy_fingerprint),
        )
        row = await cursor.fetchone()
        await conn.commit()
    return int(row[0])


async def update_policy_experiment(
    experiment_id: int,
    *,
    status: str,
    evaluation: Optional[dict] = None,
    reason: Optional[str] = None,
    model_version: Optional[str] = None,
    db_path: str = DB_PATH,
) -> None:
    """Advance a policy experiment while retaining its immutable policy payload."""
    now = datetime.utcnow().isoformat()
    fields = ["status = ?"]
    values: list = [status]
    if evaluation is not None:
        fields.append("evaluation_json = ?")
        values.append(json.dumps(evaluation, sort_keys=True))
    if reason is not None:
        fields.append("reason = ?")
        values.append(reason)
    if model_version is not None:
        fields.append("model_version = ?")
        values.append(model_version)
    if status == "forward_demo":
        fields.append("forward_started_at = ?")
        values.append(now)
    elif status in {"promoted", "rejected", "archived", "completed"}:
        fields.append("completed_at = ?")
        values.append(now)
    values.append(experiment_id)
    async with aiosqlite.connect(db_path) as conn:
        await conn.execute(f"UPDATE policy_experiments SET {', '.join(fields)} WHERE id = ?", values)
        await conn.commit()


async def get_policy_experiment(experiment_id: int, db_path: str = DB_PATH) -> Optional[dict]:
    async with aiosqlite.connect(db_path) as conn:
        conn.row_factory = aiosqlite.Row
        cursor = await conn.execute("SELECT * FROM policy_experiments WHERE id = ?", (experiment_id,))
        row = await cursor.fetchone()
    if not row:
        return None
    result = dict(row)
    result["policy"] = json.loads(result.pop("policy_json") or "{}")
    result["evaluation"] = json.loads(result.pop("evaluation_json") or "{}")
    return result


async def get_active_forward_experiment(account_mode: str = "demo", db_path: str = DB_PATH) -> Optional[dict]:
    async with aiosqlite.connect(db_path) as conn:
        conn.row_factory = aiosqlite.Row
        cursor = await conn.execute(
            """SELECT * FROM policy_experiments WHERE account_mode = ? AND status = 'forward_demo'
               ORDER BY forward_started_at DESC, created_at DESC LIMIT 1""",
            (account_mode,),
        )
        row = await cursor.fetchone()
    if not row:
        return None
    result = dict(row)
    result["policy"] = json.loads(result.pop("policy_json") or "{}")
    result["evaluation"] = json.loads(result.pop("evaluation_json") or "{}")
    return result


async def list_policy_experiments(account_mode: str = "demo", limit: int = 20, db_path: str = DB_PATH) -> list[dict]:
    async with aiosqlite.connect(db_path) as conn:
        conn.row_factory = aiosqlite.Row
        cursor = await conn.execute(
            """SELECT * FROM policy_experiments WHERE account_mode = ?
               ORDER BY created_at DESC LIMIT ?""", (account_mode, limit)
        )
        rows = [dict(row) for row in await cursor.fetchall()]
    for row in rows:
        row["policy"] = json.loads(row.pop("policy_json") or "{}")
        row["evaluation"] = json.loads(row.pop("evaluation_json") or "{}")
    return rows


async def get_pending_counterfactual_setups(
    account_mode: str,
    symbol: str,
    timeframe: str,
    db_path: str = DB_PATH,
) -> list[dict]:
    """Return rejected/failed setups with a defined hypothetical SL and TP."""
    statuses = ("rejected", "risk_rejected", "sizing_rejected", "execution_failed", "invalidated")
    placeholders = ", ".join("?" for _ in statuses)
    async with aiosqlite.connect(db_path) as conn:
        conn.row_factory = aiosqlite.Row
        cursor = await conn.execute(
            f"""SELECT * FROM setup_records
                WHERE account_mode = ? AND symbol = ? AND timeframe = ?
                  AND status IN ({placeholders})
                  AND entry_price IS NOT NULL AND stop_loss IS NOT NULL AND take_profit IS NOT NULL
                ORDER BY detected_at ASC""",
            (account_mode, symbol, timeframe, *statuses),
        )
        rows = [dict(row) for row in await cursor.fetchall()]
    for row in rows:
        row["features"] = json.loads(row.pop("features_json") or "{}")
        row["outcome"] = json.loads(row.pop("outcome_json") or "{}")
    return rows


async def get_open_trades(account_mode: str = "demo", db_path: str = DB_PATH) -> list[dict]:
    async with aiosqlite.connect(db_path) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute("SELECT * FROM trades WHERE status = 'open' AND account_mode = ? ORDER BY timestamp DESC", (account_mode,))
        rows = await cursor.fetchall()
        return [dict(r) for r in rows]


async def get_today_trade_count(account_mode: str = "demo", db_path: str = DB_PATH) -> int:
    today = date.today().isoformat()
    async with aiosqlite.connect(db_path) as db:
        cursor = await db.execute(
            "SELECT COUNT(*) FROM trades WHERE timestamp >= ? AND account_mode = ?",
            (today, account_mode)
        )
        row = await cursor.fetchone()
        return row[0] if row else 0


async def get_today_pnl(account_mode: str = "demo", db_path: str = DB_PATH) -> float:
    today = date.today().isoformat()
    async with aiosqlite.connect(db_path) as db:
        cursor = await db.execute(
            "SELECT COALESCE(SUM(pnl), 0) FROM trades WHERE timestamp >= ? AND status = 'closed' AND account_mode = ?",
            (today, account_mode)
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


async def get_trade_history(limit: int = 20, account_mode: str = "demo", db_path: str = DB_PATH) -> list[dict]:
    async with aiosqlite.connect(db_path) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            "SELECT * FROM trades WHERE account_mode = ? ORDER BY timestamp DESC LIMIT ?", (account_mode, limit)
        )
        rows = await cursor.fetchall()
        return [dict(r) for r in rows]


async def get_recent_trades(days: int = 7, account_mode: str = "demo", db_path: str = DB_PATH) -> list[dict]:
    """Fetch trades from one account mode only."""
    since = (datetime.utcnow() - timedelta(days=days)).isoformat()
    async with aiosqlite.connect(db_path) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            "SELECT * FROM trades WHERE timestamp >= ? AND account_mode = ? ORDER BY timestamp ASC, id ASC", (since, account_mode)
        )
        rows = await cursor.fetchall()
        return [dict(r) for r in rows]


async def get_policy_trade_outcomes(
    *,
    account_mode: str = "demo",
    days: int = 365,
    policy_version: Optional[str] = None,
    experiment_id: Optional[int] = None,
    db_path: str = DB_PATH,
) -> list[dict]:
    """Return completed R outcomes joined to their observed setup features.

    It is deliberately outcome-only: synthetic counterfactual results remain in
    setup records and must never be blended into broker-realized performance.
    """
    since = (datetime.utcnow() - timedelta(days=days)).isoformat()
    clauses = [
        "t.account_mode = ?", "t.status = 'closed'", "t.pnl_r IS NOT NULL", "t.timestamp >= ?",
        "(t.ticket IS NULL OR t.ticket NOT IN (SELECT ticket FROM capital_reduction_actions WHERE ticket IS NOT NULL AND action = 'order_filled'))",
    ]
    values: list = [account_mode, since]
    if policy_version is not None:
        clauses.append("t.policy_version = ?")
        values.append(policy_version)
    if experiment_id is not None:
        clauses.append("t.experiment_id = ?")
        values.append(experiment_id)
    async with aiosqlite.connect(db_path) as conn:
        conn.row_factory = aiosqlite.Row
        cursor = await conn.execute(
            f"""SELECT t.*, s.features_json, s.validation_json
                FROM trades t LEFT JOIN setup_records s ON s.id = t.setup_id
                WHERE {' AND '.join(clauses)} ORDER BY t.timestamp ASC, t.id ASC""",
            values,
        )
        rows = [dict(row) for row in await cursor.fetchall()]
    for row in rows:
        row["features"] = json.loads(row.pop("features_json") or "{}")
        row["validation"] = json.loads(row.pop("validation_json") or "{}")
    return rows


async def get_management_learning_observations(
    *, account_mode: str = "demo", days: int = 365, symbol: Optional[str] = None, db_path: str = DB_PATH,
) -> list[dict]:
    """Return completed broker-confirmed management observations.

    Capital-reduction tickets are excluded. This is read-only evidence for the
    adaptive TP/SL journal; it never produces an execution instruction.
    """
    since = (datetime.utcnow() - timedelta(days=max(1, int(days)))).isoformat()
    clauses = [
        "t.account_mode = ?", "t.status = 'closed'", "t.pnl_r IS NOT NULL", "t.timestamp >= ?",
        "(t.ticket IS NULL OR t.ticket NOT IN (SELECT ticket FROM capital_reduction_actions WHERE ticket IS NOT NULL AND action = 'order_filled'))",
    ]
    values: list[Any] = [account_mode, since]
    if symbol:
        clauses.append("t.symbol = ?")
        values.append(str(symbol))
    async with aiosqlite.connect(db_path) as conn:
        conn.row_factory = aiosqlite.Row
        cursor = await conn.execute(
            f"""SELECT t.*, s.setup_type, s.timeframe
                FROM trades t LEFT JOIN setup_records s ON s.id = t.setup_id
                WHERE {' AND '.join(clauses)} ORDER BY t.timestamp ASC, t.id ASC""",
            values,
        )
        rows = [dict(row) for row in await cursor.fetchall()]
    for row in rows:
        row["management_actions"] = await get_trade_logs(
            ticket=int(row["ticket"]) if row.get("ticket") is not None else None,
            trade_id=int(row["id"]), db_path=db_path,
        )
    return rows


async def get_management_learning_summary(
    *, account_mode: str = "demo", days: int = 365, symbol: Optional[str] = None, db_path: str = DB_PATH,
) -> dict:
    """Summarize adaptive TP/SL evidence from only persisted closed outcomes."""
    from analysis.adaptive_management import observation_from_broker_trade, summarize_management

    rows = await get_management_learning_observations(
        account_mode=account_mode, days=days, symbol=symbol, db_path=db_path
    )
    observations = [
        observation_from_broker_trade(row, row.get("management_actions") or []) for row in rows
    ]
    return summarize_management(observations)


async def create_objective_draft(
    *, account_mode: str, raw_instruction: str, objective: dict, account_snapshot: dict,
    broker_universe: list[str], context: dict, db_path: str = DB_PATH,
) -> dict:
    """Persist one non-active objective draft; it has no execution authority."""
    now = datetime.utcnow().isoformat()
    async with aiosqlite.connect(db_path) as conn:
        conn.row_factory = aiosqlite.Row
        await conn.execute(
            "UPDATE trading_objectives SET status = 'cancelled', cancelled_at = ? WHERE account_mode = ? AND status = 'draft'",
            (now, account_mode),
        )
        cursor = await conn.execute(
            """INSERT INTO trading_objectives
               (account_mode, status, raw_instruction, objective_json, account_snapshot_json,
                broker_universe_json, context_json, created_at)
               VALUES (?, 'draft', ?, ?, ?, ?, ?, ?)""",
            (
                account_mode, raw_instruction, json.dumps(objective, sort_keys=True),
                json.dumps(account_snapshot, sort_keys=True), json.dumps(broker_universe, sort_keys=True),
                json.dumps(context, sort_keys=True), now,
            ),
        )
        await conn.commit()
        cursor = await conn.execute("SELECT * FROM trading_objectives WHERE id = ?", (cursor.lastrowid,))
        return await _objective_row(await cursor.fetchone())


async def _objective_row(row) -> Optional[dict]:
    if row is None:
        return None
    result = dict(row)
    for key, fallback in (("objective_json", {}), ("account_snapshot_json", {}), ("broker_universe_json", []), ("context_json", {})):
        try:
            result[key[:-5]] = json.loads(result.pop(key) or json.dumps(fallback))
        except (TypeError, ValueError, json.JSONDecodeError):
            result[key[:-5]] = fallback
    result["is_paused"] = bool(result.get("is_paused"))
    return result


async def get_objective_draft(account_mode: str = "demo", db_path: str = DB_PATH) -> Optional[dict]:
    async with aiosqlite.connect(db_path) as conn:
        conn.row_factory = aiosqlite.Row
        cursor = await conn.execute(
            "SELECT * FROM trading_objectives WHERE account_mode = ? AND status = 'draft' ORDER BY id DESC LIMIT 1",
            (account_mode,),
        )
        return await _objective_row(await cursor.fetchone())


async def get_active_objective(account_mode: str = "demo", db_path: str = DB_PATH) -> Optional[dict]:
    async with aiosqlite.connect(db_path) as conn:
        conn.row_factory = aiosqlite.Row
        cursor = await conn.execute(
            "SELECT * FROM trading_objectives WHERE account_mode = ? AND status = 'active' ORDER BY version DESC, id DESC LIMIT 1",
            (account_mode,),
        )
        return await _objective_row(await cursor.fetchone())


async def confirm_objective_draft(
    account_mode: str = "demo", *, objective: Optional[dict] = None,
    account_snapshot: Optional[dict] = None, broker_universe: Optional[list[str]] = None,
    context: Optional[dict] = None, db_path: str = DB_PATH,
) -> Optional[dict]:
    """Activate the latest stored draft and preserve any prior objective as history."""
    now = datetime.utcnow().isoformat()
    async with aiosqlite.connect(db_path) as conn:
        conn.row_factory = aiosqlite.Row
        cursor = await conn.execute(
            "SELECT * FROM trading_objectives WHERE account_mode = ? AND status = 'draft' ORDER BY id DESC LIMIT 1",
            (account_mode,),
        )
        draft = await cursor.fetchone()
        if draft is None:
            return None
        cursor = await conn.execute(
            "SELECT COALESCE(MAX(version), 0) FROM trading_objectives WHERE account_mode = ?", (account_mode,)
        )
        version = int((await cursor.fetchone())[0] or 0) + 1
        await conn.execute(
            "UPDATE trading_objectives SET status = 'superseded', superseded_at = ? WHERE account_mode = ? AND status = 'active'",
            (now, account_mode),
        )
        await conn.execute(
            """UPDATE trading_objectives
               SET status = 'active', version = ?, confirmed_at = ?, is_paused = 0,
                   objective_json = ?, account_snapshot_json = ?, broker_universe_json = ?, context_json = ?
               WHERE id = ?""",
            (
                version, now,
                json.dumps(objective if objective is not None else json.loads(draft["objective_json"] or "{}"), sort_keys=True),
                json.dumps(account_snapshot if account_snapshot is not None else json.loads(draft["account_snapshot_json"] or "{}"), sort_keys=True),
                json.dumps(broker_universe if broker_universe is not None else json.loads(draft["broker_universe_json"] or "[]"), sort_keys=True),
                json.dumps(context if context is not None else json.loads(draft["context_json"] or "{}"), sort_keys=True),
                int(draft["id"]),
            ),
        )
        await conn.commit()
        cursor = await conn.execute("SELECT * FROM trading_objectives WHERE id = ?", (int(draft["id"]),))
        return await _objective_row(await cursor.fetchone())


async def cancel_objective_draft(account_mode: str = "demo", db_path: str = DB_PATH) -> bool:
    now = datetime.utcnow().isoformat()
    async with aiosqlite.connect(db_path) as conn:
        cursor = await conn.execute(
            "UPDATE trading_objectives SET status = 'cancelled', cancelled_at = ? WHERE account_mode = ? AND status = 'draft'",
            (now, account_mode),
        )
        await conn.commit()
        return cursor.rowcount > 0


async def set_objective_paused(account_mode: str = "demo", paused: bool = True, db_path: str = DB_PATH) -> bool:
    active = await get_active_objective(account_mode, db_path)
    if not active:
        return False
    operational = dict((active.get("context") or {}).get("operational") or {})
    # A terminal objective must be superseded by a newly confirmed objective;
    # /resume may not silently restart it after success or failure.
    if not paused and operational.get("terminal"):
        return False
    async with aiosqlite.connect(db_path) as conn:
        cursor = await conn.execute(
            "UPDATE trading_objectives SET is_paused = ? WHERE id = ? AND status = 'active'",
            (1 if paused else 0, int(active["id"])),
        )
        await conn.commit()
        return cursor.rowcount > 0


async def mark_active_objective_terminal(
    *, account_mode: str, outcome: str, terminal_state: str, demo_session_id: int,
    terminal_equity: float, reason: str, db_path: str = DB_PATH,
) -> Optional[dict]:
    """Pause the active objective at a broker-confirmed terminal outcome.

    The terminal marker keeps the completed objective visible in the console but
    prevents /objective resume from restarting it. A new explicit objective must
    be drafted and confirmed for a new objective session.
    """
    active = await get_active_objective(account_mode, db_path)
    if not active:
        return None
    context = dict(active.get("context") or {})
    operational = dict(context.get("operational") or {})
    terminal = dict(operational.get("terminal") or {})
    if terminal.get("demo_session_id") == int(demo_session_id):
        return active
    terminal = {
        "outcome": str(outcome), "state": str(terminal_state),
        "demo_session_id": int(demo_session_id), "equity": float(terminal_equity),
        "reason": str(reason), "recorded_at": datetime.utcnow().isoformat(),
    }
    operational["terminal"] = terminal
    operational["status"] = "TERMINAL"
    context["operational"] = operational
    async with aiosqlite.connect(db_path) as conn:
        conn.row_factory = aiosqlite.Row
        await conn.execute(
            "UPDATE trading_objectives SET is_paused = 1, context_json = ? WHERE id = ? AND status = 'active'",
            (json.dumps(context, sort_keys=True), int(active["id"])),
        )
        await conn.commit()
        cursor = await conn.execute("SELECT * FROM trading_objectives WHERE id = ?", (int(active["id"]),))
        return await _objective_row(await cursor.fetchone())


async def list_objective_history(account_mode: str = "demo", limit: int = 10, db_path: str = DB_PATH) -> list[dict]:
    async with aiosqlite.connect(db_path) as conn:
        conn.row_factory = aiosqlite.Row
        cursor = await conn.execute(
            "SELECT * FROM trading_objectives WHERE account_mode = ? AND status != 'draft' ORDER BY id DESC LIMIT ?",
            (account_mode, max(1, int(limit))),
        )
        return [row for item in await cursor.fetchall() if (row := await _objective_row(item)) is not None]


async def _objective_phase_row(row) -> Optional[dict]:
    if row is None:
        return None
    result = dict(row)
    result["policy_snapshot"] = json.loads(result.pop("policy_snapshot_json") or "{}")
    result["instruments"] = json.loads(result.pop("instruments_json") or "[]")
    result["metrics"] = json.loads(result.pop("metrics_json") or "{}")
    return result


async def create_objective_phase_plan(
    *, objective_id: int, demo_session_id: Optional[int], starting_equity: float,
    phase_targets: list[float], policy_snapshot: Optional[dict], instruments: list[str],
    db_path: str = DB_PATH,
) -> list[dict]:
    """Persist one immutable milestone plan and activate its first phase."""
    if not phase_targets:
        raise ValueError("Objective phase plan requires at least one target")
    now = datetime.utcnow().isoformat()
    prior = float(starting_equity)
    async with aiosqlite.connect(db_path) as conn:
        conn.row_factory = aiosqlite.Row
        for number, target in enumerate(phase_targets, start=1):
            active = number == 1
            await conn.execute(
                """INSERT INTO objective_phases
                   (objective_id, demo_session_id, phase_number, status, planned_start_equity,
                    starting_equity, target_equity, started_at, policy_snapshot_json, instruments_json)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    int(objective_id), demo_session_id, number, "active" if active else "planned",
                    prior, float(starting_equity) if active else None, float(target), now if active else None,
                    json.dumps(policy_snapshot or {}, sort_keys=True), json.dumps(instruments, sort_keys=True),
                ),
            )
            prior = float(target)
        await conn.commit()
        cursor = await conn.execute(
            "SELECT * FROM objective_phases WHERE objective_id = ? ORDER BY phase_number ASC", (int(objective_id),)
        )
        return [item for row in await cursor.fetchall() if (item := await _objective_phase_row(row)) is not None]


async def get_active_objective_phase(objective_id: int, db_path: str = DB_PATH) -> Optional[dict]:
    async with aiosqlite.connect(db_path) as conn:
        conn.row_factory = aiosqlite.Row
        cursor = await conn.execute(
            "SELECT * FROM objective_phases WHERE objective_id = ? AND status = 'active' ORDER BY phase_number ASC LIMIT 1",
            (int(objective_id),),
        )
        return await _objective_phase_row(await cursor.fetchone())


async def get_objective_phase(phase_id: int, db_path: str = DB_PATH) -> Optional[dict]:
    async with aiosqlite.connect(db_path) as conn:
        conn.row_factory = aiosqlite.Row
        cursor = await conn.execute("SELECT * FROM objective_phases WHERE id = ?", (int(phase_id),))
        return await _objective_phase_row(await cursor.fetchone())


async def list_objective_phases(objective_id: int, db_path: str = DB_PATH) -> list[dict]:
    async with aiosqlite.connect(db_path) as conn:
        conn.row_factory = aiosqlite.Row
        cursor = await conn.execute(
            "SELECT * FROM objective_phases WHERE objective_id = ? ORDER BY phase_number ASC", (int(objective_id),)
        )
        return [item for row in await cursor.fetchall() if (item := await _objective_phase_row(row)) is not None]


async def update_active_objective_context(objective_id: int, context: dict, db_path: str = DB_PATH) -> Optional[dict]:
    """Replace active objective context after a durable phase lifecycle change."""
    async with aiosqlite.connect(db_path) as conn:
        conn.row_factory = aiosqlite.Row
        await conn.execute(
            "UPDATE trading_objectives SET context_json = ? WHERE id = ? AND status = 'active'",
            (json.dumps(context, sort_keys=True), int(objective_id)),
        )
        await conn.commit()
        cursor = await conn.execute("SELECT * FROM trading_objectives WHERE id = ?", (int(objective_id),))
        return await _objective_row(await cursor.fetchone())


async def objective_phase_summary(phase_id: int, db_path: str = DB_PATH) -> dict:
    """Return phase-attributed evidence from recorded setups, closed trades, and management logs."""
    async with aiosqlite.connect(db_path) as conn:
        conn.row_factory = aiosqlite.Row
        cursor = await conn.execute(
            """SELECT trade.timestamp, trade.closed_at, trade.symbol, setup.setup_type, trade.pnl, trade.pnl_r,
                      trade.max_favorable_r, trade.max_adverse_r, trade.policy_version
               FROM trades trade LEFT JOIN setup_records setup ON setup.id = trade.setup_id
               WHERE trade.objective_phase_id = ? AND trade.status = 'closed'
               ORDER BY trade.timestamp ASC, trade.id ASC""",
            (int(phase_id),),
        )
        trades = [dict(row) for row in await cursor.fetchall()]
        cursor = await conn.execute(
            "SELECT COUNT(*) FROM setup_records WHERE objective_phase_id = ?", (int(phase_id),)
        )
        setup_count = int((await cursor.fetchone())[0] or 0)
        cursor = await conn.execute(
            """SELECT action, COUNT(*) AS count
               FROM trade_logs log JOIN trade_baskets basket
                    ON basket.objective_phase_id = ?
               JOIN trade_layers layer ON layer.basket_id = basket.id AND layer.ticket = log.ticket
               GROUP BY action ORDER BY count DESC, action ASC""",
            (int(phase_id),),
        )
        actions = {str(row["action"]): int(row["count"]) for row in await cursor.fetchall()}

    pnls = [float(row.get("pnl") or 0.0) for row in trades]
    r_values = [float(row["pnl_r"]) for row in trades if row.get("pnl_r") is not None]
    wins = [value for value in pnls if value > 0]
    losses = [value for value in pnls if value < 0]
    gross_profit, gross_loss = sum(wins), abs(sum(losses))
    running, peak, max_drawdown = 0.0, 0.0, 0.0
    curve = []
    for row, pnl in zip(trades, pnls):
        running += pnl
        peak = max(peak, running)
        max_drawdown = max(max_drawdown, peak - running)
        curve.append({"timestamp": row.get("closed_at") or row.get("timestamp"), "cumulative_pnl": running})
    per_symbol: dict[str, float] = {}
    per_setup: dict[str, float] = {}
    for row in trades:
        per_symbol[str(row.get("symbol") or "")] = per_symbol.get(str(row.get("symbol") or ""), 0.0) + float(row.get("pnl") or 0.0)
        per_setup[str(row.get("setup_type") or "unknown")] = per_setup.get(str(row.get("setup_type") or "unknown"), 0.0) + float(row.get("pnl") or 0.0)
    return {
        "setups_traded": setup_count, "trades_taken": len(trades), "wins": len(wins), "losses": len(losses),
        "win_rate": (len(wins) / len(trades) * 100) if trades else 0.0,
        "average_r": (sum(r_values) / len(r_values)) if r_values else None,
        "expectancy_r": (sum(r_values) / len(r_values)) if r_values else None,
        "profit_factor": (gross_profit / gross_loss) if gross_loss else (float("inf") if gross_profit else None),
        "maximum_drawdown": max_drawdown,
        "average_mae_r": (sum(float(row.get("max_adverse_r") or 0.0) for row in trades) / len(trades)) if trades else 0.0,
        "average_mfe_r": (sum(float(row.get("max_favorable_r") or 0.0) for row in trades) / len(trades)) if trades else 0.0,
        "management_actions": actions, "best_instrument": max(per_symbol, key=per_symbol.get) if per_symbol else None,
        "best_setup": max(per_setup, key=per_setup.get) if per_setup else None,
        "policy_versions": sorted({str(row["policy_version"]) for row in trades if row.get("policy_version")}),
        "equity_curve": curve,
    }


async def complete_objective_phase(
    phase_id: int, *, ending_equity: float, reason: str, metrics: dict,
    next_policy_snapshot: Optional[dict] = None, next_instruments: Optional[list[str]] = None,
    db_path: str = DB_PATH,
) -> tuple[Optional[dict], Optional[dict]]:
    """Freeze an active phase and atomically begin its preplanned successor, if any."""
    now = datetime.utcnow().isoformat()
    async with aiosqlite.connect(db_path) as conn:
        conn.row_factory = aiosqlite.Row
        cursor = await conn.execute("SELECT * FROM objective_phases WHERE id = ?", (int(phase_id),))
        current = await cursor.fetchone()
        if current is None or current["status"] != "active":
            return await _objective_phase_row(current), None
        await conn.execute(
            """UPDATE objective_phases SET status = 'completed', ending_equity = ?, completed_at = ?,
               completion_reason = ?, metrics_json = ? WHERE id = ?""",
            (float(ending_equity), now, str(reason), json.dumps(metrics, sort_keys=True), int(phase_id)),
        )
        cursor = await conn.execute(
            "SELECT * FROM objective_phases WHERE objective_id = ? AND phase_number = ?",
            (int(current["objective_id"]), int(current["phase_number"]) + 1),
        )
        successor = await cursor.fetchone()
        if successor is not None:
            await conn.execute(
                """UPDATE objective_phases SET status = 'active', starting_equity = ?, started_at = ?,
                   policy_snapshot_json = ?, instruments_json = ? WHERE id = ?""",
                (float(ending_equity), now, json.dumps(next_policy_snapshot or {}, sort_keys=True),
                 json.dumps(next_instruments or [], sort_keys=True), int(successor["id"])),
            )
        await conn.commit()
        cursor = await conn.execute("SELECT * FROM objective_phases WHERE id = ?", (int(phase_id),))
        completed = await _objective_phase_row(await cursor.fetchone())
        next_phase = None
        if successor is not None:
            cursor = await conn.execute("SELECT * FROM objective_phases WHERE id = ?", (int(successor["id"]),))
            next_phase = await _objective_phase_row(await cursor.fetchone())
        return completed, next_phase


async def fail_objective_phase(
    phase_id: int, *, ending_equity: float, reason: str, metrics: dict,
    db_path: str = DB_PATH,
) -> Optional[dict]:
    now = datetime.utcnow().isoformat()
    async with aiosqlite.connect(db_path) as conn:
        conn.row_factory = aiosqlite.Row
        await conn.execute(
            """UPDATE objective_phases SET status = 'failed', ending_equity = ?, completed_at = ?,
               completion_reason = ?, metrics_json = ? WHERE id = ? AND status = 'active'""",
            (float(ending_equity), now, str(reason), json.dumps(metrics, sort_keys=True), int(phase_id)),
        )
        await conn.commit()
        cursor = await conn.execute("SELECT * FROM objective_phases WHERE id = ?", (int(phase_id),))
        return await _objective_phase_row(await cursor.fetchone())


async def get_performance_summary(account_mode: str, days: Optional[int] = None, db_path: str = DB_PATH) -> dict:
    """Compute closed-trade performance for exactly one account mode."""
    clauses = [
        "status = 'closed'", "account_mode = ?",
        "(ticket IS NULL OR ticket NOT IN (SELECT ticket FROM capital_reduction_actions WHERE ticket IS NOT NULL AND action = 'order_filled'))",
    ]
    values: list = [account_mode]
    if days is not None:
        clauses.append("timestamp >= ?")
        values.append((datetime.utcnow() - timedelta(days=days)).isoformat())

    async with aiosqlite.connect(db_path) as conn:
        conn.row_factory = aiosqlite.Row
        cursor = await conn.execute(
            f"SELECT timestamp, pnl FROM trades WHERE {' AND '.join(clauses)} ORDER BY timestamp ASC",
            values,
        )
        rows = [dict(row) for row in await cursor.fetchall()]

    pnls = [float(row.get("pnl") or 0.0) for row in rows]
    wins = [value for value in pnls if value > 0]
    losses = [value for value in pnls if value < 0]
    gross_profit = sum(wins)
    gross_loss = abs(sum(losses))
    running_pnl = 0.0
    high_water = 0.0
    max_drawdown = 0.0
    for pnl in pnls:
        running_pnl += pnl
        high_water = max(high_water, running_pnl)
        max_drawdown = max(max_drawdown, high_water - running_pnl)

    return {
        "account_mode": account_mode,
        "trades": len(pnls),
        "wins": len(wins),
        "losses": len(losses),
        "pnl": sum(pnls),
        "win_rate": (len(wins) / len(pnls) * 100) if pnls else 0.0,
        "average_pnl": (sum(pnls) / len(pnls)) if pnls else 0.0,
        "profit_factor": (gross_profit / gross_loss) if gross_loss else (float("inf") if gross_profit else 0.0),
        "max_drawdown": max_drawdown,
    }


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
    account_mode: str = "demo",
    policy_version: Optional[str] = None,
    experiment_id: Optional[int] = None,
    objective_phase_id: Optional[int] = None,
    db_path: str = DB_PATH,
) -> int:
    """Persist one setup-level risk budget and its future layer plan."""
    now = datetime.utcnow().isoformat()
    async with aiosqlite.connect(db_path) as conn:
        cursor = await conn.execute(
            """INSERT INTO trade_baskets
               (created_at, updated_at, symbol, direction, entry_price, initial_stop,
                initial_target, max_risk, reserved_risk, account_mode, planned_layers, metadata,
                policy_version, experiment_id, objective_phase_id)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
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
                account_mode,
                json.dumps(planned_layers),
                json.dumps(metadata or {}),
                policy_version,
                experiment_id,
                objective_phase_id,
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


async def get_basket_for_ticket(ticket: int, account_mode: str = "demo", db_path: str = DB_PATH) -> Optional[dict]:
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
               WHERE l.ticket = ? AND b.status = 'open' AND b.account_mode = ?
               ORDER BY l.id DESC LIMIT 1""",
            (ticket, account_mode),
        )
        row = await cursor.fetchone()
        if not row:
            return None
        result = dict(row)
        result["planned_layers"] = json.loads(result["planned_layers"])
        result["metadata"] = json.loads(result["metadata"] or "{}")
        return result


async def get_open_baskets(account_mode: str = "demo", db_path: str = DB_PATH) -> list[dict]:
    """Return open baskets for one account mode with planned layer metadata."""
    async with aiosqlite.connect(db_path) as conn:
        conn.row_factory = aiosqlite.Row
        cursor = await conn.execute("SELECT * FROM trade_baskets WHERE status = 'open' AND account_mode = ? ORDER BY created_at ASC", (account_mode,))
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


async def get_consecutive_losses(limit: int = 50, account_mode: str = "demo", db_path: str = DB_PATH) -> int:
    """Return the current closed-trade loss streak for one account mode."""
    async with aiosqlite.connect(db_path) as conn:
        cursor = await conn.execute(
            """SELECT pnl FROM trades
               WHERE status = 'closed' AND account_mode = ?
               ORDER BY timestamp DESC LIMIT ?""",
            (account_mode, limit),
        )
        rows = await cursor.fetchall()
    streak = 0
    for (pnl,) in rows:
        if float(pnl or 0) < 0:
            streak += 1
        else:
            break
    return streak


async def create_capital_reduction_session(
    *,
    broker_login: str,
    target_equity: float,
    tolerance: float,
    initial_equity: float,
    initial_balance: float,
    metadata: Optional[dict] = None,
    account_mode: str = "demo",
    db_path: str = DB_PATH,
) -> int:
    """Create an isolated DEMO reduction session; it never creates a strategy trade."""
    now = datetime.utcnow().isoformat()
    async with aiosqlite.connect(db_path) as conn:
        cursor = await conn.execute(
            """INSERT INTO capital_reduction_sessions
               (account_mode, broker_login, status, target_equity, tolerance, initial_equity,
                initial_balance, current_equity, current_balance, started_at, updated_at, metadata_json)
               VALUES (?, ?, 'active', ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                account_mode, str(broker_login), float(target_equity), float(tolerance),
                float(initial_equity), float(initial_balance), float(initial_equity),
                float(initial_balance), now, now, json.dumps(metadata or {}, sort_keys=True),
            ),
        )
        await conn.commit()
        return int(cursor.lastrowid)


async def get_active_capital_reduction_session(
    account_mode: str = "demo", db_path: str = DB_PATH,
) -> Optional[dict]:
    async with aiosqlite.connect(db_path) as conn:
        conn.row_factory = aiosqlite.Row
        cursor = await conn.execute(
            """SELECT * FROM capital_reduction_sessions
               WHERE account_mode = ? AND status IN ('active', 'paused')
               ORDER BY id DESC LIMIT 1""",
            (account_mode,),
        )
        row = await cursor.fetchone()
    if not row:
        return None
    result = dict(row)
    result["metadata"] = json.loads(result.pop("metadata_json") or "{}")
    result["capital_test_active"] = bool(result.get("capital_test_active"))
    return result


async def get_capital_reduction_session(session_id: int, db_path: str = DB_PATH) -> Optional[dict]:
    async with aiosqlite.connect(db_path) as conn:
        conn.row_factory = aiosqlite.Row
        cursor = await conn.execute("SELECT * FROM capital_reduction_sessions WHERE id = ?", (session_id,))
        row = await cursor.fetchone()
    if not row:
        return None
    result = dict(row)
    result["metadata"] = json.loads(result.pop("metadata_json") or "{}")
    result["capital_test_active"] = bool(result.get("capital_test_active"))
    return result


async def update_capital_reduction_session(
    session_id: int,
    *,
    status: Optional[str] = None,
    current_equity: Optional[float] = None,
    current_balance: Optional[float] = None,
    error_reason: Optional[str] = None,
    capital_test_active: Optional[bool] = None,
    metadata: Optional[dict] = None,
    db_path: str = DB_PATH,
) -> None:
    """Update session state without writing to normal trading/learning tables."""
    fields = ["updated_at = ?"]
    values: list[Any] = [datetime.utcnow().isoformat()]
    if status is not None:
        fields.append("status = ?")
        values.append(status)
        if status == "paused":
            fields.append("paused_at = ?")
            values.append(datetime.utcnow().isoformat())
        if status in {"completed", "cancelled", "blocked", "failed"}:
            fields.append("completed_at = ?")
            values.append(datetime.utcnow().isoformat())
    if current_equity is not None:
        fields.append("current_equity = ?")
        values.append(float(current_equity))
    if current_balance is not None:
        fields.append("current_balance = ?")
        values.append(float(current_balance))
    if error_reason is not None:
        fields.append("error_reason = ?")
        values.append(str(error_reason))
    if capital_test_active is not None:
        fields.append("capital_test_active = ?")
        values.append(int(bool(capital_test_active)))
    if metadata is not None:
        fields.append("metadata_json = ?")
        values.append(json.dumps(metadata, sort_keys=True))
    values.append(int(session_id))
    async with aiosqlite.connect(db_path) as conn:
        await conn.execute(f"UPDATE capital_reduction_sessions SET {', '.join(fields)} WHERE id = ?", values)
        await conn.commit()


async def record_capital_reduction_action(
    *,
    session_id: int,
    action: str,
    status: str,
    symbol: Optional[str] = None,
    direction: Optional[str] = None,
    volume: Optional[float] = None,
    entry_price: Optional[float] = None,
    ticket: Optional[int] = None,
    equity_before: Optional[float] = None,
    equity_after: Optional[float] = None,
    details: Optional[dict] = None,
    db_path: str = DB_PATH,
) -> int:
    now = datetime.utcnow().isoformat()
    async with aiosqlite.connect(db_path) as conn:
        cursor = await conn.execute(
            """INSERT INTO capital_reduction_actions
               (session_id, created_at, action, status, symbol, direction, volume, entry_price,
                ticket, equity_before, equity_after, details_json)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                int(session_id), now, action, status, symbol, direction, volume, entry_price,
                ticket, equity_before, equity_after, json.dumps(details or {}, sort_keys=True),
            ),
        )
        await conn.commit()
        return int(cursor.lastrowid)


async def get_capital_reduction_actions(
    session_id: int, limit: int = 100, db_path: str = DB_PATH,
) -> list[dict]:
    async with aiosqlite.connect(db_path) as conn:
        conn.row_factory = aiosqlite.Row
        cursor = await conn.execute(
            "SELECT * FROM capital_reduction_actions WHERE session_id = ? ORDER BY id DESC LIMIT ?",
            (int(session_id), max(1, int(limit))),
        )
        rows = [dict(row) for row in await cursor.fetchall()]
    for row in rows:
        row["details"] = json.loads(row.pop("details_json") or "{}")
    return rows


async def get_latest_capital_reduction_session(
    account_mode: str = "demo", db_path: str = DB_PATH,
) -> Optional[dict]:
    async with aiosqlite.connect(db_path) as conn:
        conn.row_factory = aiosqlite.Row
        cursor = await conn.execute(
            "SELECT * FROM capital_reduction_sessions WHERE account_mode = ? ORDER BY id DESC LIMIT 1",
            (account_mode,),
        )
        row = await cursor.fetchone()
    if not row:
        return None
    result = dict(row)
    result["metadata"] = json.loads(result.pop("metadata_json") or "{}")
    result["capital_test_active"] = bool(result.get("capital_test_active"))
    return result


async def is_capital_reduction_ticket(ticket: int, db_path: str = DB_PATH) -> bool:
    """Identify isolated reduction tickets so telemetry never treats them as strategy evidence."""
    async with aiosqlite.connect(db_path) as conn:
        cursor = await conn.execute(
            "SELECT 1 FROM capital_reduction_actions WHERE ticket = ? AND action = 'order_filled' LIMIT 1",
            (int(ticket),),
        )
        return await cursor.fetchone() is not None


async def get_strategy_trade_outcomes_excluding_capital_reduction(
    account_mode: str = "demo", days: int = 365, db_path: str = DB_PATH,
) -> list[dict]:
    """Return strategy outcomes while explicitly excluding capital-reduction tickets."""
    since = (datetime.utcnow() - timedelta(days=days)).isoformat()
    async with aiosqlite.connect(db_path) as conn:
        conn.row_factory = aiosqlite.Row
        cursor = await conn.execute(
            """SELECT t.* FROM trades t
               WHERE t.account_mode = ? AND t.status = 'closed' AND t.timestamp >= ?
                 AND (t.ticket IS NULL OR t.ticket NOT IN (
                    SELECT ticket FROM capital_reduction_actions
                    WHERE ticket IS NOT NULL AND action = 'order_filled'
                 ))
               ORDER BY t.timestamp ASC, t.id ASC""",
            (account_mode, since),
        )
        return [dict(row) for row in await cursor.fetchall()]


async def get_account_state(account_mode: str = "demo", db_path: str = DB_PATH) -> Optional[dict]:
    async with aiosqlite.connect(db_path) as conn:
        conn.row_factory = aiosqlite.Row
        cursor = await conn.execute("SELECT * FROM account_state WHERE account_mode = ?", (account_mode,))
        row = await cursor.fetchone()
    if not row:
        return None
    result = dict(row)
    result["metadata"] = json.loads(result.pop("metadata_json") or "{}")
    return result


async def upsert_account_state(
    *,
    account_mode: str,
    broker_login: Optional[str],
    state: str,
    balance: Optional[float],
    equity: Optional[float],
    free_margin: Optional[float],
    margin_level: Optional[float],
    minimum_operating_capital: Optional[float],
    active_demo_session_id: Optional[int],
    exhaustion_reason: Optional[str] = None,
    reset_previous_balance: Optional[float] = None,
    reset_previous_equity: Optional[float] = None,
    reset_detected_at: Optional[str] = None,
    notification_key: Optional[str] = None,
    metadata: Optional[dict] = None,
    db_path: str = DB_PATH,
) -> None:
    """Persist one authoritative broker-account state row per account mode."""
    now = datetime.utcnow().isoformat()
    previous = await get_account_state(account_mode, db_path)
    changed_at = now if not previous or previous.get("state") != state else previous.get("state_changed_at", now)
    async with aiosqlite.connect(db_path) as conn:
        await conn.execute(
            """INSERT INTO account_state
               (account_mode, broker_login, state, state_changed_at, last_checked_at,
                last_balance, last_equity, last_free_margin, last_margin_level,
                minimum_operating_capital, active_demo_session_id, exhaustion_reason,
                reset_previous_balance, reset_previous_equity, reset_detected_at,
                notification_key, metadata_json)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(account_mode) DO UPDATE SET
                broker_login=excluded.broker_login, state=excluded.state,
                state_changed_at=excluded.state_changed_at, last_checked_at=excluded.last_checked_at,
                last_balance=excluded.last_balance, last_equity=excluded.last_equity,
                last_free_margin=excluded.last_free_margin, last_margin_level=excluded.last_margin_level,
                minimum_operating_capital=excluded.minimum_operating_capital,
                active_demo_session_id=excluded.active_demo_session_id,
                exhaustion_reason=excluded.exhaustion_reason,
                reset_previous_balance=excluded.reset_previous_balance,
                reset_previous_equity=excluded.reset_previous_equity,
                reset_detected_at=excluded.reset_detected_at,
                notification_key=excluded.notification_key, metadata_json=excluded.metadata_json""",
            (
                account_mode, broker_login, state, changed_at, now, balance, equity,
                free_margin, margin_level, minimum_operating_capital,
                active_demo_session_id, exhaustion_reason, reset_previous_balance,
                reset_previous_equity, reset_detected_at, notification_key,
                json.dumps(metadata or {}, sort_keys=True),
            ),
        )
        await conn.commit()


async def create_demo_session(
    *, broker_login: str, start_balance: float, start_equity: float,
    capital_reduction_activity: bool = False, capital_test_active: bool = False,
    db_path: str = DB_PATH,
) -> int:
    now = datetime.utcnow().isoformat()
    async with aiosqlite.connect(db_path) as conn:
        cursor = await conn.execute(
            """INSERT INTO demo_sessions
               (broker_login, status, started_at, start_balance, start_equity, max_equity,
                min_equity, capital_reduction_activity, capital_test_active)
               VALUES (?, 'active', ?, ?, ?, ?, ?, ?, ?)""",
            (str(broker_login), now, float(start_balance), float(start_equity),
             float(start_equity), float(start_equity), int(capital_reduction_activity), int(capital_test_active)),
        )
        await conn.commit()
        return int(cursor.lastrowid)


async def get_demo_session(session_id: int, db_path: str = DB_PATH) -> Optional[dict]:
    async with aiosqlite.connect(db_path) as conn:
        conn.row_factory = aiosqlite.Row
        cursor = await conn.execute("SELECT * FROM demo_sessions WHERE id = ?", (int(session_id),))
        row = await cursor.fetchone()
    if not row:
        return None
    result = dict(row)
    result["policy_versions"] = json.loads(result.pop("policy_versions_json") or "[]")
    result["capital_reduction_activity"] = bool(result.get("capital_reduction_activity"))
    result["capital_test_active"] = bool(result.get("capital_test_active"))
    return result


async def get_active_demo_session(broker_login: str, db_path: str = DB_PATH) -> Optional[dict]:
    async with aiosqlite.connect(db_path) as conn:
        conn.row_factory = aiosqlite.Row
        cursor = await conn.execute(
            "SELECT * FROM demo_sessions WHERE broker_login = ? AND status = 'active' ORDER BY id DESC LIMIT 1",
            (str(broker_login),),
        )
        row = await cursor.fetchone()
    if not row:
        return None
    result = dict(row)
    result["policy_versions"] = json.loads(result.pop("policy_versions_json") or "[]")
    result["capital_reduction_activity"] = bool(result.get("capital_reduction_activity"))
    result["capital_test_active"] = bool(result.get("capital_test_active"))
    return result


async def update_demo_session_equity(
    session_id: int, *, balance: float, equity: float, capital_test_active: Optional[bool] = None,
    db_path: str = DB_PATH,
) -> None:
    current = await get_demo_session(session_id, db_path)
    if not current:
        return
    maximum = max(float(current.get("max_equity") or equity), float(equity))
    minimum = min(float(current.get("min_equity") or equity), float(equity))
    drawdown = max(0.0, (maximum - minimum) / maximum * 100) if maximum else 0.0
    fields = ["end_balance = ?", "end_equity = ?", "max_equity = ?", "min_equity = ?", "max_drawdown_pct = ?"]
    values: list[Any] = [float(balance), float(equity), maximum, minimum, drawdown]
    if capital_test_active is not None:
        fields.append("capital_test_active = ?")
        values.append(int(bool(capital_test_active)))
    values.append(int(session_id))
    async with aiosqlite.connect(db_path) as conn:
        await conn.execute(f"UPDATE demo_sessions SET {', '.join(fields)} WHERE id = ?", values)
        await conn.commit()


async def close_demo_session(
    session_id: int, *, status: str, balance: float, equity: float,
    exhaustion_reason: Optional[str] = None, reset_detected_at: Optional[str] = None,
    db_path: str = DB_PATH,
) -> None:
    await update_demo_session_equity(session_id, balance=balance, equity=equity, db_path=db_path)
    await refresh_demo_session_metrics(session_id, db_path)
    async with aiosqlite.connect(db_path) as conn:
        await conn.execute(
            """UPDATE demo_sessions SET status = ?, ended_at = ?, end_balance = ?, end_equity = ?,
               exhaustion_reason = ?, reset_detected_at = ? WHERE id = ?""",
            (status, datetime.utcnow().isoformat(), float(balance), float(equity), exhaustion_reason, reset_detected_at, int(session_id)),
        )
        await conn.commit()


async def claim_objective_session_review(
    *, demo_session_id: int, objective_id: Optional[int], outcome: str, terminal_state: str,
    summary: Optional[dict] = None, db_path: str = DB_PATH,
) -> bool:
    """Claim the one permitted post-session review for a terminal DEMO session."""
    async with aiosqlite.connect(db_path) as conn:
        cursor = await conn.execute(
            """INSERT OR IGNORE INTO objective_session_reviews
               (demo_session_id, objective_id, outcome, terminal_state, created_at, summary_json)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (int(demo_session_id), objective_id, str(outcome), str(terminal_state),
             datetime.utcnow().isoformat(), json.dumps(summary or {}, sort_keys=True)),
        )
        await conn.commit()
        return cursor.rowcount > 0


async def complete_objective_session_review(
    demo_session_id: int, *, summary: Optional[dict] = None, optimization: Optional[dict] = None,
    db_path: str = DB_PATH,
) -> None:
    async with aiosqlite.connect(db_path) as conn:
        await conn.execute(
            """UPDATE objective_session_reviews
               SET completed_at = ?, summary_json = ?, optimization_json = ?
               WHERE demo_session_id = ?""",
            (datetime.utcnow().isoformat(), json.dumps(summary or {}, sort_keys=True),
             json.dumps(optimization or {}, sort_keys=True), int(demo_session_id)),
        )
        await conn.commit()


async def get_objective_session_review(demo_session_id: int, db_path: str = DB_PATH) -> Optional[dict]:
    async with aiosqlite.connect(db_path) as conn:
        conn.row_factory = aiosqlite.Row
        cursor = await conn.execute(
            "SELECT * FROM objective_session_reviews WHERE demo_session_id = ?", (int(demo_session_id),)
        )
        row = await cursor.fetchone()
    if not row:
        return None
    result = dict(row)
    result["summary"] = json.loads(result.pop("summary_json") or "{}")
    result["optimization"] = json.loads(result.pop("optimization_json") or "{}")
    return result


async def record_account_state_event(
    *, account_mode: str, broker_login: Optional[str], demo_session_id: Optional[int],
    event_type: str, state: str, balance: Optional[float], equity: Optional[float],
    free_margin: Optional[float], margin_level: Optional[float], minimum_operating_capital: Optional[float],
    details: Optional[dict] = None, db_path: str = DB_PATH,
) -> int:
    async with aiosqlite.connect(db_path) as conn:
        cursor = await conn.execute(
            """INSERT INTO account_state_events
               (account_mode, broker_login, demo_session_id, event_type, state, created_at,
                balance, equity, free_margin, margin_level, minimum_operating_capital, details_json)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (account_mode, broker_login, demo_session_id, event_type, state,
             datetime.utcnow().isoformat(), balance, equity, free_margin,
             margin_level, minimum_operating_capital, json.dumps(details or {}, sort_keys=True)),
        )
        await conn.commit()
        return int(cursor.lastrowid)


async def get_recent_account_state_events(account_mode: str = "demo", limit: int = 20, db_path: str = DB_PATH) -> list[dict]:
    async with aiosqlite.connect(db_path) as conn:
        conn.row_factory = aiosqlite.Row
        cursor = await conn.execute(
            "SELECT * FROM account_state_events WHERE account_mode = ? ORDER BY id DESC LIMIT ?",
            (account_mode, max(1, int(limit))),
        )
        rows = [dict(row) for row in await cursor.fetchall()]
    for row in rows:
        row["details"] = json.loads(row.pop("details_json") or "{}")
    return rows


async def refresh_demo_session_metrics(session_id: int, db_path: str = DB_PATH) -> dict:
    """Aggregate only normal strategy trades assigned to one DEMO session."""
    async with aiosqlite.connect(db_path) as conn:
        conn.row_factory = aiosqlite.Row
        cursor = await conn.execute(
            """SELECT pnl, pnl_r, symbol, policy_version FROM trades
               WHERE demo_session_id = ? AND status = 'closed'
                 AND (ticket IS NULL OR ticket NOT IN (
                    SELECT ticket FROM capital_reduction_actions
                    WHERE ticket IS NOT NULL AND action = 'order_filled'
                 ))
               ORDER BY timestamp ASC, id ASC""",
            (int(session_id),),
        )
        rows = [dict(row) for row in await cursor.fetchall()]
    pnls = [float(row.get("pnl") or 0.0) for row in rows]
    r_values = [float(row.get("pnl_r") or 0.0) for row in rows if row.get("pnl_r") is not None]
    wins = [value for value in pnls if value > 0]
    losses = [value for value in pnls if value < 0]
    gross_profit = sum(wins)
    gross_loss = abs(sum(losses))
    factor = gross_profit / gross_loss if gross_loss else (float("inf") if gross_profit else None)
    policies = sorted({str(row["policy_version"]) for row in rows if row.get("policy_version")})
    metrics = {
        "strategy_trades": len(rows), "wins": len(wins), "losses": len(losses),
        "profit_factor": factor, "expectancy_r": sum(r_values) / len(r_values) if r_values else None,
        "policy_versions": policies,
    }
    async with aiosqlite.connect(db_path) as conn:
        await conn.execute(
            """UPDATE demo_sessions SET strategy_trades = ?, wins = ?, losses = ?,
               profit_factor = ?, expectancy_r = ?, policy_versions_json = ? WHERE id = ?""",
            (metrics["strategy_trades"], metrics["wins"], metrics["losses"], metrics["profit_factor"],
             metrics["expectancy_r"], json.dumps(policies), int(session_id)),
        )
        await conn.commit()
    return metrics


async def get_demo_session_report(session_id: int, db_path: str = DB_PATH) -> Optional[dict]:
    session = await get_demo_session(session_id, db_path)
    if not session:
        return None
    await refresh_demo_session_metrics(session_id, db_path)
    return await get_demo_session(session_id, db_path)


async def get_demo_session_symbol_summary(session_id: int, db_path: str = DB_PATH) -> list[dict]:
    """Return normal strategy P/L by market for one reset-separated DEMO session."""
    async with aiosqlite.connect(db_path) as conn:
        conn.row_factory = aiosqlite.Row
        cursor = await conn.execute(
            """SELECT symbol, COUNT(*) AS trades, COALESCE(SUM(pnl), 0) AS pnl,
                      COALESCE(AVG(pnl_r), 0) AS avg_r
               FROM trades
               WHERE demo_session_id = ? AND status = 'closed'
                 AND (ticket IS NULL OR ticket NOT IN (
                    SELECT ticket FROM capital_reduction_actions
                    WHERE ticket IS NOT NULL AND action = 'order_filled'
                 ))
               GROUP BY symbol ORDER BY pnl DESC, trades DESC""",
            (int(session_id),),
        )
        return [dict(row) for row in await cursor.fetchall()]
