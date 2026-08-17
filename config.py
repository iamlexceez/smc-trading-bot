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
    # RR policy is explicit and configurable. Actual RR is always calculated;
    # a zero minimum is a deliberate request to disable RR-only filtering.
    rr_filter_enabled: bool = True
    min_rr_ratio: float = 2.0
    preferred_rr_ratio: float = 3.0
    # Quality ranks already-valid setups; it never replaces or relaxes a
    # structural validity gate. A zero floor allows DEMO research to observe
    # all structurally valid archetypes before evidence sets a soft threshold.
    score_threshold: float = 0.0
    min_setup_score: float = 0.0
    extreme_setup_score: float = 90.0
    max_spread_pips: float = 5.0
    symbol_cooldown_minutes: int = 5
    virtual_balance: Optional[float] = None
    aggressive_mode: bool = True
    index_focus: bool = False
    target_balance: Optional[float] = None

    # DEMO capital-reduction controls. These are operational session inputs,
    # never optimizer parameters and never used for normal strategy sizing.
    capital_reduction_target: Optional[float] = None
    capital_reduction_tolerance: float = 10.0
    # Optional target-relative component; effective DEMO reduction tolerance is
    # max(absolute tolerance, target equity × this percentage).
    capital_reduction_tolerance_pct: float = 0.0
    # Aggressive DEMO reduction may cross below target only inside this separate
    # lower-bound tolerance; it is never used by normal strategy execution.
    capital_reduction_overshoot_tolerance: float = 0.0
    capital_reduction_overshoot_tolerance_pct: float = 100.0

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

    # Autonomous execution. DEMO begins enabled after the broker market
    # universe and all risk checks are verified; LIVE remains separately gated.
    autonomous_learning_mode: bool = True
    auto_trade: bool = True
    # Controlled DEMO exploration is explicit policy configuration. It never
    # authorizes LIVE exposure and never bypasses downstream broker/risk gates.
    exploration_enabled: bool = True
    exploration_min_setup_score: float = 80.0
    exploration_min_strategy_score: float = 80.0
    exploration_risk_multiplier: float = 0.5
    is_paused: bool = False
    # Explains why bot-wide automation is paused. This prevents verified
    # broker recovery or standalone-scope activation from overriding a
    # deliberate manual or emergency pause, while allowing stale safety pauses
    # from older sessions to recover deterministically.
    automation_pause_reason: str = ""
    # A verified external DEMO reset remains paused unless this explicit
    # operational opt-in is enabled.
    demo_auto_resume_after_reset: bool = False
    live_trading_confirmed_at: Optional[str] = None

    # Chart-activity notifications are event-driven from closed broker candles.
    # Detailed mode includes study and rejection events; duplicates are keyed
    # by symbol, stage, and closed bar to avoid scan-heartbeat spam.
    chart_activity_notifications: bool = True
    chart_activity_level: str = "detailed"  # off | essential | detailed
    chart_activity_include_rejections: bool = True
    chart_activity_cooldown_seconds: int = 300

    # Instruments are populated from the connected Deriv MT5 account only.
    # No forex ticker or guessed Gold symbol is retained as a default.
    symbols: list[str] = field(default_factory=list)
    enabled_symbols: list[str] = field(default_factory=list)
    available_symbols: list[str] = field(default_factory=list)
    unsupported_symbols: list[str] = field(default_factory=list)
    symbol_status: dict[str, str] = field(default_factory=dict)
    market_universe_updated_at: Optional[str] = None
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

    # Bounded learning and model governance. The optimizer may adjust only
    # soft parameters inside the hard caps declared above.
    self_optimization_enabled: bool = True
    optimization_interval_days: int = 1
    optimization_min_sample_size: int = 30
    optimization_min_split_size: int = 10
    optimization_min_improvement: float = 0.05
    optimization_rollback_tolerance: float = 0.20
    preferred_risk_pct: float = 0.75
    preferred_max_trades_per_day: int = 10
    active_model_version: str = ""
    # Research governance. The research cohort and earned Core universe are
    # separate concepts: a research cohort may explore several names, while
    # Core is capped at ten and may contain fewer than ten instruments.
    research_market_limit: int = 10
    max_core_instruments: int = 10
    core_min_sample_size: int = 50
    core_adjusted_score_threshold: float = 75.0
    core_max_drawdown_r: float = 2.0
    core_min_execution_reliability: float = 0.95
    market_ranking_min_sample_size: int = 10
    market_ranking_lookback_days: int = 365
    strategy_ranking_limit: int = 3
    strategy_ranking_min_sample_size: int = 10
    ranking_tie_threshold: float = 2.0
    daily_report_hour_utc: int = 7
    daily_report_minute_utc: int = 0
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
        for key in ("symbols", "enabled_symbols", "available_symbols", "unsupported_symbols", "timeframes", "htf_timeframes", "enabled_sessions", "news_impact_levels", "layer_allocation"):
            if isinstance(d.get(key), list):
                d[key] = ",".join(str(value) for value in d[key])
        
        # Complex objects → JSON
        if isinstance(d.get("brokers"), list):
            d["brokers"] = json.dumps([asdict(b) for b in self.brokers])
        d["symbol_status"] = json.dumps(d.get("symbol_status", {}), sort_keys=True)

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

        # Persisted symbol lists may originate from a legacy forex or generic
        # market configuration.  Never restore them: the scheduler must rebuild
        # every active/displayed symbol from the connected Deriv MT5 account.
        symbols_list: list[str] = []
        enabled_symbols_list: list[str] = []
        available_symbols_list: list[str] = []
        unsupported_symbols_list: list[str] = []

        import json
        symbol_status_raw = d.get("symbol_status", "{}")
        try:
            symbol_status = json.loads(symbol_status_raw) if isinstance(symbol_status_raw, str) else dict(symbol_status_raw or {})
        except (TypeError, ValueError, json.JSONDecodeError):
            symbol_status = {}
        # Status metadata is broker-derived as well and must not surface a stale
        # instrument list before startup discovery completes.
        symbol_status = {}
        
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
            # Experimental policy discovery mode: Risk, RR, daily limits, and sizing
            # are fully variable and discovered by the optimizer rather than clamped.
            risk_per_trade=float(d.get("risk_per_trade", 0.75)),
            max_setup_risk_pct=float(d.get("max_setup_risk_pct", 5.0)),
            max_daily_loss_pct=float(d.get("max_daily_loss_pct", 3.0)),
            absolute_daily_stop_pct=float(d.get("absolute_daily_stop_pct", 4.0)),
            daily_pnl_limit_pct=float(d.get("daily_pnl_limit_pct", 20.0)),
            daily_profit_lock_pct=float(d.get("daily_profit_lock_pct", 5.0)),
            daily_profit_stop_pct=float(d.get("daily_profit_stop_pct", 10.0)),
            max_total_open_risk_pct=float(d.get("max_total_open_risk_pct", 3.0)),
            max_consecutive_losses=int(d.get("max_consecutive_losses", 3)),
            max_trades_per_day=int(d.get("max_trades_per_day", 10)),
            max_open_positions=int(d.get("max_open_positions", 2)),
            rr_filter_enabled=parse_bool(d.get("rr_filter_enabled", "true"), True),
            min_rr_ratio=max(0.0, float(d.get("min_rr_ratio", 2.0))),
            preferred_rr_ratio=max(0.0, float(d.get("preferred_rr_ratio", 3.0))),
            score_threshold=max(0.0, float(d.get("score_threshold", 0.0))),
            min_setup_score=max(0.0, float(d.get("min_setup_score", 0.0))),
            extreme_setup_score=float(d.get("extreme_setup_score", 90.0)),
            max_spread_pips=float(d.get("max_spread_pips", 5.0)),
            symbol_cooldown_minutes=int(d.get("symbol_cooldown_minutes", 5)),
            virtual_balance=float(d["virtual_balance"]) if d.get("virtual_balance") else None,
            aggressive_mode=parse_bool(d.get("aggressive_mode", "true"), True),
            index_focus=parse_bool(d.get("index_focus", "false")),
            target_balance=float(d["target_balance"]) if d.get("target_balance") else None,
            capital_reduction_target=float(d["capital_reduction_target"]) if d.get("capital_reduction_target") else None,
            capital_reduction_tolerance=max(0.0, float(d.get("capital_reduction_tolerance", 10.0))),
            capital_reduction_tolerance_pct=max(0.0, float(d.get("capital_reduction_tolerance_pct", 0.0))),
            capital_reduction_overshoot_tolerance=max(0.0, float(d.get("capital_reduction_overshoot_tolerance", 0.0))),
            capital_reduction_overshoot_tolerance_pct=max(0.0, float(d.get("capital_reduction_overshoot_tolerance_pct", 100.0))),
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
            autonomous_learning_mode=parse_bool(d.get("autonomous_learning_mode", "true"), True),
            # Existing installations are deliberately migrated into the new
            # demo learning default once. Later explicit pause/stop controls
            # retain their saved setting.
            auto_trade=(parse_bool(d.get("auto_trade", "true"), True) if "autonomous_learning_mode" in d else True),
            is_paused=parse_bool(d.get("is_paused", "false")),
            automation_pause_reason=str(d.get("automation_pause_reason") or ""),
            demo_auto_resume_after_reset=parse_bool(d.get("demo_auto_resume_after_reset", "false")),
            live_trading_confirmed_at=d.get("live_trading_confirmed_at"),
            chart_activity_notifications=parse_bool(d.get("chart_activity_notifications", "true"), True),
            chart_activity_level=(str(d.get("chart_activity_level", "detailed")).lower() if str(d.get("chart_activity_level", "detailed")).lower() in {"off", "essential", "detailed"} else "detailed"),
            chart_activity_include_rejections=parse_bool(d.get("chart_activity_include_rejections", "true"), True),
            chart_activity_cooldown_seconds=max(30, int(d.get("chart_activity_cooldown_seconds", 300))),
            symbols=symbols_list,
            enabled_symbols=enabled_symbols_list,
            available_symbols=available_symbols_list,
            unsupported_symbols=unsupported_symbols_list,
            symbol_status={str(key): str(value) for key, value in symbol_status.items()},
            market_universe_updated_at=d.get("market_universe_updated_at"),
            timeframes=parse_list(d.get("timeframes"), ["M15", "H1", "H4"]),
            htf_timeframes=parse_list(d.get("htf_timeframes"), ["H1", "H4", "D1"]),
            expert_mode=parse_bool(d.get("expert_mode", "false")),
            scalping_mode=parse_bool(d.get("scalping_mode", "false")),
            trading_mode=("live" if str(d.get("trading_mode", "demo")).lower() == "live" and d.get("live_trading_confirmed_at") else "demo"),
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
            self_optimization_enabled=parse_bool(d.get("self_optimization_enabled", "true"), True),
            optimization_interval_days=max(1, int(d.get("optimization_interval_days", 1))),
            optimization_min_sample_size=max(10, int(d.get("optimization_min_sample_size", 30))),
            optimization_min_split_size=max(5, int(d.get("optimization_min_split_size", 10))),
            optimization_min_improvement=max(0.0, float(d.get("optimization_min_improvement", 0.05))),
            optimization_rollback_tolerance=max(0.0, float(d.get("optimization_rollback_tolerance", 0.20))),
            preferred_risk_pct=float(d.get("preferred_risk_pct", d.get("risk_per_trade", 0.75))),
            preferred_max_trades_per_day=max(1, int(d.get("preferred_max_trades_per_day", d.get("max_trades_per_day", 10)))),
            active_model_version=d.get("active_model_version", ""),
            research_market_limit=max(1, int(d.get("research_market_limit", 10))),
            max_core_instruments=max(0, min(10, int(d.get("max_core_instruments", 10)))),
            core_min_sample_size=max(1, int(d.get("core_min_sample_size", 50))),
            core_adjusted_score_threshold=max(0.0, min(100.0, float(d.get("core_adjusted_score_threshold", 75.0)))),
            core_max_drawdown_r=max(0.0, float(d.get("core_max_drawdown_r", 2.0))),
            core_min_execution_reliability=max(0.0, min(1.0, float(d.get("core_min_execution_reliability", 0.95)))),
            market_ranking_min_sample_size=max(1, int(d.get("market_ranking_min_sample_size", 10))),
            market_ranking_lookback_days=max(1, int(d.get("market_ranking_lookback_days", 365))),
            strategy_ranking_limit=max(1, min(3, int(d.get("strategy_ranking_limit", 3)))),
            strategy_ranking_min_sample_size=max(1, int(d.get("strategy_ranking_min_sample_size", 10))),
            ranking_tie_threshold=max(0.0, float(d.get("ranking_tie_threshold", 2.0))),
            daily_report_hour_utc=max(0, min(23, int(d.get("daily_report_hour_utc", 7)))),
            daily_report_minute_utc=max(0, min(59, int(d.get("daily_report_minute_utc", 0)))),
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
            rr_filter_enabled=os.getenv("RR_FILTER_ENABLED", "true").lower() == "true",
            min_rr_ratio=max(0.0, float(os.getenv("MIN_RR", "2.0"))),
            preferred_rr_ratio=max(0.0, float(os.getenv("PREFERRED_RR", "3.0"))),
            score_threshold=float(os.getenv("SCORE_THRESHOLD", "0.0")),
            min_setup_score=float(os.getenv("MIN_SETUP_SCORE", "0.0")),
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
            autonomous_learning_mode=True,
            auto_trade=os.getenv("AUTO_TRADE", "true").lower() == "true",
            is_paused=False,
            live_trading_confirmed_at=None,
            chart_activity_notifications=os.getenv("CHART_ACTIVITY_NOTIFICATIONS", "true").lower() == "true",
            chart_activity_level=os.getenv("CHART_ACTIVITY_LEVEL", "detailed").lower(),
            chart_activity_include_rejections=os.getenv("CHART_ACTIVITY_INCLUDE_REJECTIONS", "true").lower() == "true",
            chart_activity_cooldown_seconds=max(30, int(os.getenv("CHART_ACTIVITY_COOLDOWN_SECONDS", "300"))),
            symbols=[],
            enabled_symbols=[],
            available_symbols=[],
            unsupported_symbols=[],
            symbol_status={},
            market_universe_updated_at=None,
            timeframes=[s.strip() for s in os.getenv("TIMEFRAMES", "M15,H1,H4").split(",")],
            htf_timeframes=[s.strip() for s in os.getenv("HTF_TIMEFRAMES", "H1,H4,D1").split(",")],
            research_market_limit=max(1, int(os.getenv("RESEARCH_MARKET_LIMIT", "10"))),
            max_core_instruments=max(0, min(10, int(os.getenv("MAX_CORE_INSTRUMENTS", "10")))),
            core_min_sample_size=max(1, int(os.getenv("CORE_MIN_SAMPLE_SIZE", "50"))),
            core_adjusted_score_threshold=max(0.0, min(100.0, float(os.getenv("CORE_ADJUSTED_SCORE_THRESHOLD", "75.0")))),
            core_max_drawdown_r=max(0.0, float(os.getenv("CORE_MAX_DRAWDOWN_R", "2.0"))),
            core_min_execution_reliability=max(0.0, min(1.0, float(os.getenv("CORE_MIN_EXECUTION_RELIABILITY", "0.95")))),
            market_ranking_min_sample_size=max(1, int(os.getenv("MARKET_RANKING_MIN_SAMPLE_SIZE", "10"))),
            market_ranking_lookback_days=max(1, int(os.getenv("MARKET_RANKING_LOOKBACK_DAYS", "365"))),
            strategy_ranking_limit=max(1, min(3, int(os.getenv("STRATEGY_RANKING_LIMIT", "3")))),
            strategy_ranking_min_sample_size=max(1, int(os.getenv("STRATEGY_RANKING_MIN_SAMPLE_SIZE", "10"))),
            ranking_tie_threshold=max(0.0, float(os.getenv("RANKING_TIE_THRESHOLD", "2.0"))),
            trading_mode="demo",
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
            enabled_sessions=[s.strip() for s in os.getenv("ENABLED_SESSIONS", "all").split(",")],
            news_filter_enabled=os.getenv("NEWS_FILTER_ENABLED", "true").lower() == "true",
            news_impact_levels=[s.strip() for s in os.getenv("NEWS_IMPACT_LEVELS", "High").split(",")],
            news_blackout_minutes=int(os.getenv("NEWS_BLACKOUT_MINUTES", "15")),
        )
