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


def get_mt5_credentials(mode: str = "demo") -> dict:
    """Get MT5 credentials for the specified mode (demo or live)."""
    prefix = "MT5_LIVE" if mode == "live" else "MT5_DEMO"
    
    # Fallback to generic MT5_ keys if mode-specific ones are missing (for backward compatibility)
    login = os.getenv(f"{prefix}_LOGIN") or os.getenv("MT5_LOGIN")
    password = os.getenv(f"{prefix}_PASSWORD") or os.getenv("MT5_PASSWORD")
    server = os.getenv(f"{prefix}_SERVER") or os.getenv("MT5_SERVER")
    path = os.getenv(f"{prefix}_PATH") or os.getenv("MT5_PATH")
    
    return {
        "login": int(login) if login and login.isdigit() else None,
        "password": password or "",
        "server": server or "",
        "path": path or None,
    }


def get_trading_mode() -> str:
    return os.getenv("TRADING_MODE", "demo").lower()


# ─── Tradeable Settings (persisted in DB, adjustable via Telegram) ──

@dataclass
class TradeSettings:
    # Risk
    risk_per_trade: float = 1.0
    max_daily_loss_pct: float = 5.0
    max_trades_per_day: int = 10
    max_open_positions: int = 5
    min_rr_ratio: float = 3.0
    score_threshold: float = 60.0  # Raised from 40 to 60
    max_spread_pips: float = 5.0
    symbol_cooldown_minutes: int = 30
    virtual_balance: Optional[float] = None
    aggressive_mode: bool = False
    index_focus: bool = False
    target_balance: Optional[float] = None

    # Auto-trade
    auto_trade: bool = False
    is_paused: bool = False

    # Instruments
    symbols: list[str] = field(default_factory=lambda: [
        "EURUSD", "GBPUSD", "USDJPY", "XAUUSD",
        "Volatility 75 Index", "Boom 500 Index", "Crash 500 Index",
    ])
    enabled_symbols: list[str] = field(default_factory=lambda: [
        "EURUSD", "GBPUSD", "USDJPY", "XAUUSD",
        "Volatility 75 Index", "Boom 500 Index", "Crash 500 Index",
    ])
    timeframes: list[str] = field(default_factory=lambda: ["M15", "H1", "H4"])
    htf_timeframes: list[str] = field(default_factory=lambda: ["H1", "H4", "D1"])

    # Execution
    trading_mode: str = "demo"  # demo | live
    magic_number: int = 20260807

    # Entry confirmation
    require_zone_retest: bool = True
    require_candle_confirmation: bool = True
    require_displacement: bool = True  # Turned on by default

    # Trade management
    breakeven_at_rr: float = 1.0
    trail_atr_multiplier: float = 2.0
    partial_close_rr: float = 2.0
    partial_close_percent: float = 0.50
    max_hold_bars: int = 100

    # Session filtering
    enabled_sessions: list[str] = field(default_factory=lambda: ["london", "new_york", "overlap"])

    # News filtering
    news_filter_enabled: bool = True
    news_impact_levels: list[str] = field(default_factory=lambda: ["High"])
    news_blackout_minutes: int = 15

    def to_dict(self) -> dict:
        d = asdict(self)
        # Lists → comma strings for SQLite storage
        for key in ("symbols", "enabled_symbols", "timeframes", "htf_timeframes", "enabled_sessions", "news_impact_levels"):
            if isinstance(d.get(key), list):
                d[key] = ",".join(d[key])
        return d

    @classmethod
    def from_dict(cls, d: dict) -> "TradeSettings":
        def parse_bool(val, default=False):
            return val in ("true", "1", True) if isinstance(val, str) else (val if isinstance(val, bool) else default)

        def parse_list(val, default=None):
            if isinstance(val, list):
                return val
            if isinstance(val, str):
                return [s.strip() for s in val.split(",") if s.strip()]
            return default or []

        return cls(
            risk_per_trade=float(d.get("risk_per_trade", 1.0)),
            max_daily_loss_pct=float(d.get("max_daily_loss_pct", 5.0)),
            max_trades_per_day=int(d.get("max_trades_per_day", 10)),
            max_open_positions=int(d.get("max_open_positions", 5)),
            min_rr_ratio=float(d.get("min_rr_ratio", 3.0)),
            score_threshold=float(d.get("score_threshold", 60.0)),
            max_spread_pips=float(d.get("max_spread_pips", 5.0)),
            symbol_cooldown_minutes=int(d.get("symbol_cooldown_minutes", 30)),
            virtual_balance=float(d["virtual_balance"]) if d.get("virtual_balance") else None,
            aggressive_mode=parse_bool(d.get("aggressive_mode", "false")),
            index_focus=parse_bool(d.get("index_focus", "false")),
            target_balance=float(d["target_balance"]) if d.get("target_balance") else None,
            auto_trade=parse_bool(d.get("auto_trade", "false")),
            is_paused=parse_bool(d.get("is_paused", "false")),
            symbols=parse_list(d.get("symbols")),
            enabled_symbols=parse_list(d.get("enabled_symbols"), d.get("symbols")),
            timeframes=parse_list(d.get("timeframes")),
            htf_timeframes=parse_list(d.get("htf_timeframes")),
            trading_mode=d.get("trading_mode", "demo"),
            magic_number=int(d.get("magic_number", 20260807)),
            require_zone_retest=parse_bool(d.get("require_zone_retest", "true"), True),
            require_candle_confirmation=parse_bool(d.get("require_candle_confirmation", "true"), True),
            require_displacement=parse_bool(d.get("require_displacement", "true"), True),
            breakeven_at_rr=float(d.get("breakeven_at_rr", 1.0)),
            trail_atr_multiplier=float(d.get("trail_atr_multiplier", 2.0)),
            partial_close_rr=float(d.get("partial_close_rr", 2.0)),
            partial_close_percent=float(d.get("partial_close_percent", 0.50)),
            max_hold_bars=int(d.get("max_hold_bars", 100)),
            enabled_sessions=parse_list(d.get("enabled_sessions"), ["london", "new_york", "overlap"]),
            news_filter_enabled=parse_bool(d.get("news_filter_enabled", "true"), True),
            news_impact_levels=parse_list(d.get("news_impact_levels"), ["High"]),
            news_blackout_minutes=int(d.get("news_blackout_minutes", 15)),
        )

    @classmethod
    def defaults(cls) -> "TradeSettings":
        return cls(
            risk_per_trade=float(os.getenv("RISK_PER_TRADE", "1.0")),
            max_daily_loss_pct=float(os.getenv("MAX_DAILY_LOSS_PCT", "5.0")),
            max_trades_per_day=int(os.getenv("MAX_TRADES_PER_DAY", "10")),
            max_open_positions=int(os.getenv("MAX_OPEN_POSITIONS", "5")),
            min_rr_ratio=float(os.getenv("MIN_RR_RATIO", "3.0")),
            score_threshold=float(os.getenv("SCORE_THRESHOLD", "60.0")),
            max_spread_pips=float(os.getenv("MAX_SPREAD_PIPS", "5.0")),
            symbol_cooldown_minutes=int(os.getenv("SYMBOL_COOLDOWN_MINUTES", "30")),
            virtual_balance=float(os.getenv("VIRTUAL_BALANCE")) if os.getenv("VIRTUAL_BALANCE") else None,
            aggressive_mode=os.getenv("AGGRESSIVE_MODE", "false").lower() == "true",
            index_focus=os.getenv("INDEX_FOCUS", "false").lower() == "true",
            target_balance=float(os.getenv("TARGET_BALANCE")) if os.getenv("TARGET_BALANCE") else None,
            auto_trade=os.getenv("AUTO_TRADE", "false").lower() == "true",
            is_paused=False,
            symbols=[s.strip() for s in os.getenv("SYMBOLS", "EURUSD,GBPUSD,USDJPY,XAUUSD,Volatility 75 Index,Boom 500 Index,Crash 500 Index").split(",")],
            enabled_symbols=[s.strip() for s in os.getenv("ENABLED_SYMBOLS", os.getenv("SYMBOLS", "EURUSD,GBPUSD,USDJPY,XAUUSD,Volatility 75 Index,Boom 500 Index,Crash 500 Index")).split(",")],
            timeframes=[s.strip() for s in os.getenv("TIMEFRAMES", "M15,H1,H4").split(",")],
            htf_timeframes=[s.strip() for s in os.getenv("HTF_TIMEFRAMES", "H1,H4,D1").split(",")],
            trading_mode=get_trading_mode(),
            magic_number=20260807,
            require_zone_retest=os.getenv("REQUIRE_ZONE_RETEST", "true").lower() == "true",
            require_candle_confirmation=os.getenv("REQUIRE_CANDLE_CONFIRMATION", "true").lower() == "true",
            require_displacement=os.getenv("REQUIRE_DISPLACEMENT", "true").lower() == "true",
            breakeven_at_rr=float(os.getenv("BREAKEVEN_AT_RR", "1.0")),
            trail_atr_multiplier=float(os.getenv("TRAIL_ATR_MULTIPLIER", "2.0")),
            partial_close_rr=float(os.getenv("PARTIAL_CLOSE_RR", "2.0")),
            partial_close_percent=float(os.getenv("PARTIAL_CLOSE_PERCENT", "0.50")),
            max_hold_bars=int(os.getenv("MAX_HOLD_BARS", "100")),
            enabled_sessions=[s.strip() for s in os.getenv("ENABLED_SESSIONS", "london,new_york,overlap").split(",")],
            news_filter_enabled=os.getenv("NEWS_FILTER_ENABLED", "true").lower() == "true",
            news_impact_levels=[s.strip() for s in os.getenv("NEWS_IMPACT_LEVELS", "High").split(",")],
            news_blackout_minutes=int(os.getenv("NEWS_BLACKOUT_MINUTES", "15")),
        )
