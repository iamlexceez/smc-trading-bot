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
class BrokerConfig:
    name: str
    login: int
    password: str
    server: str
    terminal_path: str
    is_active: bool = True

@dataclass
class TradeSettings:
    # Risk — risk is always a setup/basket budget, never a recovery mechanism.
    risk_per_trade: float = 0.75
    max_setup_risk_pct: float = 1.00
    max_daily_loss_pct: float = 3.0
    absolute_daily_stop_pct: float = 4.0
    daily_pnl_limit_pct: float = 20.0  # Legacy symmetric cap; retained for existing installations.
    daily_profit_lock_pct: float = 5.0
    daily_profit_stop_pct: float = 10.0
    max_total_open_risk_pct: float = 3.0
    max_consecutive_losses: int = 3
    max_trades_per_day: int = 10
    max_open_positions: int = 2
    min_rr_ratio: float = 3.0
    score_threshold: float = 75.0
    min_setup_score: float = 75.0
    extreme_setup_score: float = 90.0
    max_spread_pips: float = 5.0
    symbol_cooldown_minutes: int = 5
    virtual_balance: Optional[float] = None
    aggressive_mode: bool = True
    index_focus: bool = False
    target_balance: Optional[float] = None

    # Position/layer controls
    max_layers: int = 4
    layer_allocation: list[float] = field(default_factory=lambda: [0.40, 0.30, 0.20, 0.10])
    entry_mode: str = "aggressive"  # confirmed | aggressive | extreme
    allow_aggressive_entry: bool = True
    allow_extreme_entry: bool = False
    approval_timeout_minutes: int = 10
    max_signal_age_minutes: int = 10

    # Causal setup-validation thresholds
    liquidity_sweep_min_penetration_atr: float = 0.05
    displacement_body_ratio_min: float = 0.60
    displacement_range_ratio_min: float = 1.20
    structural_stop_atr_buffer: float = 0.15
    max_chase_distance_atr: float = 0.50

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
    expert_mode: bool = False
    scalping_mode: bool = False

    # ─── Institutional 10/10 Features ───────────────────
    # Multi-Broker Sync
    brokers: list[BrokerConfig] = field(default_factory=list)
    arbitrage_enabled: bool = False
    sync_mode: str = "mirror" # mirror | split

    # AI Sentiment Analysis
    sentiment_analysis_enabled: bool = False
    llm_provider: str = "openai" # openai | local
    sentiment_weight: float = 0.15 # Impact on total score

    # Self-Optimization AI
    self_optimization_enabled: bool = False
    optimization_interval_days: int = 7
    last_optimization_date: Optional[str] = None

    # Execution
    trading_mode: str = "demo"  # demo | live
    magic_number: int = 20260807

    # Entry confirmation
    require_zone_retest: bool = True
    require_candle_confirmation: bool = True
    require_displacement: bool = True  # Turned on by default

    # Trade management — structural state-machine controls.
    breakeven_at_rr: float = 1.0
    profit_lock_rr: float = 1.5
    runner_rr: float = 2.0
    trail_atr_multiplier: float = 2.0  # Legacy compatibility only; structure takes priority.
    trailing_buffer_atr: float = 0.15
    min_sl_update_distance_atr: float = 0.05
    min_tp_update_distance_atr: float = 0.10
    min_minutes_between_modifications: int = 5
    allow_partial_tp: bool = True
    allow_tp_extension: bool = True
    partial_close_rr: float = 2.0
    partial_close_percent: float = 0.20
    max_hold_bars: int = 100

    # Session filtering
    enabled_sessions: list[str] = field(default_factory=lambda: ["london", "new_york", "overlap"])

    # News filtering
    news_filter_enabled: bool = True
    news_impact_levels: list[str] = field(default_factory=lambda: ["High"])
    news_blackout_minutes: int = 15

    def to_dict(self) -> dict:
        import json
        d = asdict(self)
        # Lists → comma strings for SQLite storage
        for key in ("symbols", "enabled_symbols", "timeframes", "htf_timeframes", "enabled_sessions", "news_impact_levels", "layer_allocation"):
            if isinstance(d.get(key), list):
                d[key] = ",".join(str(value) for value in d[key])
        
        # Complex objects → JSON
        if isinstance(d.get("brokers"), list):
            d["brokers"] = json.dumps([asdict(b) for b in self.brokers])
            
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
            # If default is provided, parse it too if it's a string
            if default is not None:
                if isinstance(default, list):
                    return default
                if isinstance(default, str):
                    return [s.strip() for s in default.split(",") if s.strip()]
            return []

        # Parse master symbols first
        symbols_list = parse_list(d.get("symbols"))

        import json
        
        # Parse brokers
        brokers_raw = d.get("brokers", "[]")
        brokers = []
        if isinstance(brokers_raw, str):
            try:
                brokers_data = json.loads(brokers_raw)
                brokers = [BrokerConfig(**b) for b in brokers_data]
            except:
                brokers = []
        elif isinstance(brokers_raw, list):
            brokers = [BrokerConfig(**b) if isinstance(b, dict) else b for b in brokers_raw]

        return cls(
            risk_per_trade=float(d.get("risk_per_trade", 0.75)),
            max_setup_risk_pct=float(d.get("max_setup_risk_pct", 1.0)),
            max_daily_loss_pct=float(d.get("max_daily_loss_pct", 3.0)),
            absolute_daily_stop_pct=float(d.get("absolute_daily_stop_pct", 4.0)),
            daily_pnl_limit_pct=float(d.get("daily_pnl_limit_pct", 20.0)),
            daily_profit_lock_pct=float(d.get("daily_profit_lock_pct", 5.0)),
            daily_profit_stop_pct=float(d.get("daily_profit_stop_pct", 10.0)),
            max_total_open_risk_pct=float(d.get("max_total_open_risk_pct", 3.0)),
            max_consecutive_losses=int(d.get("max_consecutive_losses", 3)),
            max_trades_per_day=int(d.get("max_trades_per_day", 10)),
            max_open_positions=int(d.get("max_open_positions", 2)),
            min_rr_ratio=float(d.get("min_rr_ratio", 3.0)),
            score_threshold=float(d.get("score_threshold", 75.0)),
            min_setup_score=float(d.get("min_setup_score", d.get("score_threshold", 75.0))),
            extreme_setup_score=float(d.get("extreme_setup_score", 90.0)),
            max_spread_pips=float(d.get("max_spread_pips", 5.0)),
            symbol_cooldown_minutes=int(d.get("symbol_cooldown_minutes", 5)),
            virtual_balance=float(d["virtual_balance"]) if d.get("virtual_balance") else None,
            aggressive_mode=parse_bool(d.get("aggressive_mode", "true"), True),
            index_focus=parse_bool(d.get("index_focus", "false")),
            target_balance=float(d["target_balance"]) if d.get("target_balance") else None,
            max_layers=int(d.get("max_layers", 4)),
            layer_allocation=[float(value) for value in parse_list(d.get("layer_allocation"), ["0.40", "0.30", "0.20", "0.10"])],
            entry_mode=d.get("entry_mode", "aggressive"),
            allow_aggressive_entry=parse_bool(d.get("allow_aggressive_entry", "true"), True),
            allow_extreme_entry=parse_bool(d.get("allow_extreme_entry", "false")),
            approval_timeout_minutes=int(d.get("approval_timeout_minutes", 10)),
            max_signal_age_minutes=int(d.get("max_signal_age_minutes", 10)),
            liquidity_sweep_min_penetration_atr=float(d.get("liquidity_sweep_min_penetration_atr", 0.05)),
            displacement_body_ratio_min=float(d.get("displacement_body_ratio_min", 0.60)),
            displacement_range_ratio_min=float(d.get("displacement_range_ratio_min", 1.20)),
            structural_stop_atr_buffer=float(d.get("structural_stop_atr_buffer", 0.15)),
            max_chase_distance_atr=float(d.get("max_chase_distance_atr", 0.50)),
            auto_trade=parse_bool(d.get("auto_trade", "false")),
            is_paused=parse_bool(d.get("is_paused", "false")),
            symbols=symbols_list,
            enabled_symbols=parse_list(d.get("enabled_symbols"), symbols_list),
            timeframes=parse_list(d.get("timeframes"), ["M15", "H1", "H4"]),
            htf_timeframes=parse_list(d.get("htf_timeframes"), ["H1", "H4", "D1"]),
            expert_mode=parse_bool(d.get("expert_mode", "false")),
            scalping_mode=parse_bool(d.get("scalping_mode", "false")),
            trading_mode=d.get("trading_mode", "demo"),
            magic_number=int(d.get("magic_number", 20260807)),
            require_zone_retest=parse_bool(d.get("require_zone_retest", "true"), True),
            require_candle_confirmation=parse_bool(d.get("require_candle_confirmation", "true"), True),
            require_displacement=parse_bool(d.get("require_displacement", "true"), True),
            breakeven_at_rr=float(d.get("breakeven_at_rr", 1.0)),
            profit_lock_rr=float(d.get("profit_lock_rr", 1.5)),
            runner_rr=float(d.get("runner_rr", 2.0)),
            trail_atr_multiplier=float(d.get("trail_atr_multiplier", 2.0)),
            trailing_buffer_atr=float(d.get("trailing_buffer_atr", 0.15)),
            min_sl_update_distance_atr=float(d.get("min_sl_update_distance_atr", 0.05)),
            min_tp_update_distance_atr=float(d.get("min_tp_update_distance_atr", 0.10)),
            min_minutes_between_modifications=int(d.get("min_minutes_between_modifications", 5)),
            allow_partial_tp=parse_bool(d.get("allow_partial_tp", "true"), True),
            allow_tp_extension=parse_bool(d.get("allow_tp_extension", "true"), True),
            partial_close_rr=float(d.get("partial_close_rr", 2.0)),
            partial_close_percent=float(d.get("partial_close_percent", 0.20)),
            max_hold_bars=int(d.get("max_hold_bars", 100)),
            enabled_sessions=parse_list(d.get("enabled_sessions"), ["london", "new_york", "overlap"]),
            news_filter_enabled=parse_bool(d.get("news_filter_enabled", "true"), True),
            news_impact_levels=parse_list(d.get("news_impact_levels"), ["High"]),
            news_blackout_minutes=int(d.get("news_blackout_minutes", 15)),
            brokers=brokers,
            arbitrage_enabled=parse_bool(d.get("arbitrage_enabled", "false")),
            sync_mode=d.get("sync_mode", "mirror"),
            sentiment_analysis_enabled=parse_bool(d.get("sentiment_analysis_enabled", "false")),
            llm_provider=d.get("llm_provider", "openai"),
            sentiment_weight=float(d.get("sentiment_weight", 0.15)),
            self_optimization_enabled=parse_bool(d.get("self_optimization_enabled", "false")),
            optimization_interval_days=int(d.get("optimization_interval_days", 7)),
            last_optimization_date=d.get("last_optimization_date"),
        )

    @classmethod
    def defaults(cls) -> "TradeSettings":
        return cls(
            risk_per_trade=float(os.getenv("RISK_PER_TRADE", "0.75")),
            max_setup_risk_pct=float(os.getenv("MAX_SETUP_RISK_PCT", "1.0")),
            max_daily_loss_pct=float(os.getenv("MAX_DAILY_LOSS_PCT", "3.0")),
            absolute_daily_stop_pct=float(os.getenv("ABSOLUTE_DAILY_STOP_PCT", "4.0")),
            daily_pnl_limit_pct=float(os.getenv("DAILY_PNL_LIMIT_PCT", "20.0")),
            daily_profit_lock_pct=float(os.getenv("DAILY_PROFIT_LOCK_PCT", "5.0")),
            daily_profit_stop_pct=float(os.getenv("DAILY_PROFIT_STOP_PCT", "10.0")),
            max_total_open_risk_pct=float(os.getenv("MAX_TOTAL_OPEN_RISK_PCT", "3.0")),
            max_consecutive_losses=int(os.getenv("MAX_CONSECUTIVE_LOSSES", "3")),
            max_trades_per_day=int(os.getenv("MAX_TRADES_PER_DAY", "10")),
            max_open_positions=int(os.getenv("MAX_OPEN_POSITIONS", "2")),
            min_rr_ratio=float(os.getenv("MIN_RR_RATIO", "3.0")),
            score_threshold=float(os.getenv("SCORE_THRESHOLD", "75.0")),
            min_setup_score=float(os.getenv("MIN_SETUP_SCORE", "75.0")),
            extreme_setup_score=float(os.getenv("EXTREME_SETUP_SCORE", "90.0")),
            max_spread_pips=float(os.getenv("MAX_SPREAD_PIPS", "5.0")),
            symbol_cooldown_minutes=int(os.getenv("SYMBOL_COOLDOWN_MINUTES", "5")),
            virtual_balance=float(os.getenv("VIRTUAL_BALANCE")) if os.getenv("VIRTUAL_BALANCE") else None,
            aggressive_mode=os.getenv("AGGRESSIVE_MODE", "true").lower() == "true",
            index_focus=os.getenv("INDEX_FOCUS", "false").lower() == "true",
            target_balance=float(os.getenv("TARGET_BALANCE")) if os.getenv("TARGET_BALANCE") else None,
            max_layers=int(os.getenv("MAX_LAYERS", "4")),
            layer_allocation=[float(value.strip()) for value in os.getenv("LAYER_ALLOCATION", "0.40,0.30,0.20,0.10").split(",")],
            entry_mode=os.getenv("ENTRY_MODE", "aggressive").lower(),
            allow_aggressive_entry=os.getenv("ALLOW_AGGRESSIVE_ENTRY", "true").lower() == "true",
            allow_extreme_entry=os.getenv("ALLOW_EXTREME_ENTRY", "false").lower() == "true",
            approval_timeout_minutes=int(os.getenv("APPROVAL_TIMEOUT_MINUTES", "10")),
            max_signal_age_minutes=int(os.getenv("MAX_SIGNAL_AGE_MINUTES", "10")),
            liquidity_sweep_min_penetration_atr=float(os.getenv("LIQUIDITY_SWEEP_MIN_PENETRATION_ATR", "0.05")),
            displacement_body_ratio_min=float(os.getenv("DISPLACEMENT_BODY_RATIO_MIN", "0.60")),
            displacement_range_ratio_min=float(os.getenv("DISPLACEMENT_RANGE_RATIO_MIN", "1.20")),
            structural_stop_atr_buffer=float(os.getenv("STRUCTURAL_STOP_ATR_BUFFER", "0.15")),
            max_chase_distance_atr=float(os.getenv("MAX_CHASE_DISTANCE_ATR", "0.50")),
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
            profit_lock_rr=float(os.getenv("PROFIT_LOCK_RR", "1.5")),
            runner_rr=float(os.getenv("RUNNER_RR", "2.0")),
            trail_atr_multiplier=float(os.getenv("TRAIL_ATR_MULTIPLIER", "2.0")),
            trailing_buffer_atr=float(os.getenv("TRAILING_BUFFER_ATR", "0.15")),
            min_sl_update_distance_atr=float(os.getenv("MIN_SL_UPDATE_DISTANCE_ATR", "0.05")),
            min_tp_update_distance_atr=float(os.getenv("MIN_TP_UPDATE_DISTANCE_ATR", "0.10")),
            min_minutes_between_modifications=int(os.getenv("MIN_MINUTES_BETWEEN_MODIFICATIONS", "5")),
            allow_partial_tp=os.getenv("ALLOW_PARTIAL_TP", "true").lower() == "true",
            allow_tp_extension=os.getenv("ALLOW_TP_EXTENSION", "true").lower() == "true",
            partial_close_rr=float(os.getenv("PARTIAL_CLOSE_RR", "2.0")),
            partial_close_percent=float(os.getenv("PARTIAL_CLOSE_PERCENT", "0.20")),
            max_hold_bars=int(os.getenv("MAX_HOLD_BARS", "100")),
            enabled_sessions=[s.strip() for s in os.getenv("ENABLED_SESSIONS", "london,new_york,overlap").split(",")],
            news_filter_enabled=os.getenv("NEWS_FILTER_ENABLED", "true").lower() == "true",
            news_impact_levels=[s.strip() for s in os.getenv("NEWS_IMPACT_LEVELS", "High").split(",")],
            news_blackout_minutes=int(os.getenv("NEWS_BLACKOUT_MINUTES", "15")),
        )
