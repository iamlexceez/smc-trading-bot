"""
Configuration management for the SMC Trading Bot.
All settings are stored in SQLite and adjustable via Telegram commands.
Secrets (tokens, passwords) come from .env only — never stored in DB.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field, asdict
from typing import Optional

from dotenv import load_dotenv

load_dotenv()


# ─── Secrets (from .env only — never persisted to DB) ───────

def get_telegram_token() -> str:
    return os.getenv("TELEGRAM_BOT_TOKEN", "")


def get_admin_ids() -> list[int]:
    raw = os.getenv("TELEGRAM_ADMIN_IDS", "")
    return [int(x.strip()) for x in raw.split(",") if x.strip().isdigit()]


def get_mt5_credentials() -> dict:
    return {
        "login": int(os.getenv("MT5_LOGIN", "0") or "0") or None,
        "password": os.getenv("MT5_PASSWORD", ""),
        "server": os.getenv("MT5_SERVER", ""),
        "path": os.getenv("MT5_PATH", "") or None,
    }


def get_trading_mode() -> str:
    return os.getenv("TRADING_MODE", "paper").lower()


# ─── Tradeable Settings (persisted in DB, adjustable via Telegram) ──

@dataclass
class TradeSettings:
    # Risk
    risk_per_trade: float = 1.0
    max_daily_loss_pct: float = 5.0
    max_trades_per_day: int = 10
    max_open_positions: int = 5
    min_rr_ratio: float = 3.0
    score_threshold: float = 40.0
    max_spread_pips: float = 5.0
    symbol_cooldown_minutes: int = 30

    # Auto-trade
    auto_trade: bool = False
    is_paused: bool = False

    # Instruments
    symbols: list[str] = field(default_factory=lambda: [
        "EURUSD", "GBPUSD", "USDJPY", "XAUUSD",
        "Volatility 75 Index", "Boom 500 Index", "Crash 500 Index",
    ])
    timeframes: list[str] = field(default_factory=lambda: ["M15", "H1", "H4"])
    htf_timeframes: list[str] = field(default_factory=lambda: ["H1", "H4", "D1"])

    # Execution
    trading_mode: str = "paper"  # paper | live
    magic_number: int = 20260807

    def to_dict(self) -> dict:
        d = asdict(self)
        # Lists → comma strings for SQLite storage
        d["symbols"] = ",".join(d["symbols"])
        d["timeframes"] = ",".join(d["timeframes"])
        d["htf_timeframes"] = ",".join(d["htf_timeframes"])
        return d

    @classmethod
    def from_dict(cls, d: dict) -> "TradeSettings":
        return cls(
            risk_per_trade=float(d.get("risk_per_trade", 1.0)),
            max_daily_loss_pct=float(d.get("max_daily_loss_pct", 5.0)),
            max_trades_per_day=int(d.get("max_trades_per_day", 10)),
            max_open_positions=int(d.get("max_open_positions", 5)),
            min_rr_ratio=float(d.get("min_rr_ratio", 3.0)),
            score_threshold=float(d.get("score_threshold", 40.0)),
            max_spread_pips=float(d.get("max_spread_pips", 5.0)),
            symbol_cooldown_minutes=int(d.get("symbol_cooldown_minutes", 30)),
            auto_trade=d.get("auto_trade", "false") in ("true", "1", True),
            is_paused=d.get("is_paused", "false") in ("true", "1", True),
            symbols=[s.strip() for s in str(d.get("symbols", "")).split(",") if s.strip()],
            timeframes=[s.strip() for s in str(d.get("timeframes", "")).split(",") if s.strip()],
            htf_timeframes=[s.strip() for s in str(d.get("htf_timeframes", "")).split(",") if s.strip()],
            trading_mode=d.get("trading_mode", "paper"),
            magic_number=int(d.get("magic_number", 20260807)),
        )

    @classmethod
    def defaults(cls) -> "TradeSettings":
        return cls(
            risk_per_trade=float(os.getenv("RISK_PER_TRADE", "1.0")),
            max_daily_loss_pct=float(os.getenv("MAX_DAILY_LOSS_PCT", "5.0")),
            max_trades_per_day=int(os.getenv("MAX_TRADES_PER_DAY", "10")),
            max_open_positions=int(os.getenv("MAX_OPEN_POSITIONS", "5")),
            min_rr_ratio=float(os.getenv("MIN_RR_RATIO", "3.0")),
            score_threshold=float(os.getenv("SCORE_THRESHOLD", "40.0")),
            max_spread_pips=float(os.getenv("MAX_SPREAD_PIPS", "5.0")),
            symbol_cooldown_minutes=int(os.getenv("SYMBOL_COOLDOWN_MINUTES", "30")),
            auto_trade=os.getenv("AUTO_TRADE", "false").lower() == "true",
            is_paused=False,
            symbols=[s.strip() for s in os.getenv("SYMBOLS", "EURUSD,GBPUSD,USDJPY,XAUUSD,Volatility 75 Index,Boom 500 Index,Crash 500 Index").split(",")],
            timeframes=[s.strip() for s in os.getenv("TIMEFRAMES", "M15,H1,H4").split(",")],
            htf_timeframes=[s.strip() for s in os.getenv("HTF_TIMEFRAMES", "H1,H4,D1").split(",")],
            trading_mode=get_trading_mode(),
            magic_number=20260807,
        )
