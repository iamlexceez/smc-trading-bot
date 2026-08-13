# SMC Trading Bot — Specification

## Overview

A Telegram-controlled trading bot that uses **APA (Advanced Price Action)** and **Supply & Demand** strategies to analyze currency pairs, synthetic indices, and gold. It scores each setup using a multi-factor system, and auto-executes trades that pass a configurable threshold with a minimum 1:3 risk-reward ratio.

**⚠️ Disclaimer:** This is a configurable rule engine, not a guaranteed profitable system. Always test in paper mode first. Trading involves substantial risk of loss.

---

## Architecture

```
smc-trading-bot/
├── main.py                  # Entry point
├── config.py                # Configuration & settings model
├── scheduler.py             # Market scanner & auto-execution loop
├── analysis/
│   ├── indicators.py        # Base TA indicators (ATR, EMA, RSI, swing detection)
│   ├── structure.py         # APA market structure (BOS, CHoCH, OB, FVG, liquidity)
│   ├── supply_demand.py     # Supply/Demand zone detection & scoring
│   └── scoring.py           # Multi-factor signal scoring engine
├── executors/
│   ├── base.py              # Abstract executor interface
│   ├── paper.py             # Paper trading simulator (default)
│   └── mt5.py               # MetaTrader 5 execution
├── risk/
│   └── manager.py           # Risk gate checks & position sizing
├── storage/
│   └── db.py                # SQLite persistence (settings, trades, cooldowns)
├── bot/
│   ├── handlers.py          # Telegram command & callback handlers
│   └── keyboards.py         # Inline keyboard layouts
├── docs/
│   ├── SPEC.md              # This file
│   └── VPS_DEPLOYMENT.md    # Deployment guide
├── requirements.txt
├── .env.example
├── .gitignore
├── Dockerfile
└── docker-compose.yml
```

---

## Analysis Engine

### APA (Advanced Price Action)

| Component | Description |
|-----------|-------------|
| **Swing Detection** | Identifies swing highs/lows using configurable lookback (default 3 bars) |
| **Market Structure** | Classifies trend as Bullish (HH+HL), Bearish (LH+LL), or Ranging |
| **BOS** | Break of Structure — trend continuation signal |
| **CHoCH** | Change of Character — trend reversal signal |
| **Order Blocks** | Last opposite candle before impulse move (institutional footprints) |
| **Fair Value Gaps** | 3-candle imbalances where price hasn't returned (unfilled gaps) |
| **Liquidity Pools** | Equal highs (buy-side) and equal lows (sell-side) where stops accumulate |
| **Premium/Discount** | Equilibrium-based zone classification (upper 50% = premium, lower = discount) |

### Supply & Demand Zones

| Pattern | Type | Description |
|---------|------|-------------|
| Rally-Base-Drop | Supply | Up → consolidation → sharp drop |
| Drop-Base-Drop | Supply | Down → consolidation → sharp drop |
| Drop-Base-Rally | Demand | Down → consolidation → sharp rally |
| Rally-Base-Rally | Demand | Up → consolidation → sharp rally |

Each zone is scored on:
- **Impulse strength** (40%): size of the departure move relative to ATR
- **Freshness** (40%): whether price has returned to the zone
- **Base tightness** (20%): fewer base bars = stronger zone

---

## Scoring System

| # | Factor | Weight | Max Points |
|---|--------|--------|------------|
| 1 | Market Structure Alignment | 20% | 20 |
| 2 | S/D Zone Presence | 15% | 15 |
| 3 | Order Block Confluence | 15% | 15 |
| 4 | Fair Value Gap | 10% | 10 |
| 5 | Liquidity Sweep | 15% | 15 |
| 6 | Risk-Reward Ratio | 15% | 15 |
| 7 | MTF Confluence | 10% | 10 |
| | **Total** | **100%** | **100** |

### Scoring Details

**1. Structure Alignment (20%)**
- BOS in trade direction: 100%
- Trend aligned, no BOS: 80%
- Ranging + in correct zone (discount for BUY, premium for SELL): 40%
- Counter-trend: 0%

**2. S/D Zone (15%)**
- Fresh zone with high strength (>70): 100%
- Fresh zone with medium strength (40-70): 70%
- Mitigated zone: 30%
- No zone: 0%

**3. Order Block (15%)**
- OB within 2×ATR of price: 100%
- OB within 5×ATR: 60%
- OB further away: 30%
- No OB: 0%

**4. FVG (10%)**
- FVG within 2×ATR: 100%
- FVG within 5×ATR: 50%
- FVG further: 20%
- No FVG: 0%

**5. Liquidity Sweep (15%)**
- Pool swept within 1×ATR: 100%
- Pool within 3×ATR: 60%
- Pool further: 20%
- No pool: 0%

**6. RR Ratio (15%)**
- RR ≥ min_rr (default 3.0): 100%
- RR ≥ 70% of min: 70%
- RR ≥ 50% of min: 40%
- RR < 50%: 0%

**7. MTF Confluence (10%)**
- All HTF aligned: 100%
- Proportional to alignment ratio

---

## Risk Management — Hard Gates

Every trade must pass ALL of these checks before execution:

| Gate | Description | Default |
|------|-------------|---------|
| Auto-trade enabled | `auto_trade=true` and not paused | OFF |
| Symbol allowed | Symbol is in the configured list | — |
| Symbol cooldown | No trade on this symbol within cooldown period | 30 min |
| Daily loss limit | Today's PnL > -(balance × max_daily_loss%) | 5% |
| Daily trade count | Trades today < max_trades_per_day | 10 |
| Max open positions | Open positions < max_open_positions | 5 |
| Score threshold | Signal score ≥ threshold | 40% |
| Min RR ratio | RR ≥ min_rr_ratio | 1:3 |
| Spread check | Current spread ≤ max_spread_pips | 5 pips |
| Free margin | Free margin > 2× required margin | — |

### Position Sizing

```
lot_size = (balance × risk_per_trade%) / (SL_distance_pips × pip_value_per_lot)
```

- Preferred risk per setup: 0.75% by default
- Hard cap: 1% per setup; no automatic process can exceed it
- Portfolio-risk cap: 3%; daily-loss cap: 3%; emergency stop: 4%

---

## Supported Instruments

| Type | Eligibility |
|------|-------------|
| Deriv Synthetic Indices | Only symbols exposed as tradeable by the connected Deriv MT5 account. |
| Deriv Gold | Only a Gold symbol exposed as tradeable by the connected Deriv MT5 account. |
| Other markets | Explicitly unsupported and excluded from scans, backtests, and execution. |

---

## Execution Modes

### DEMO Mode (default)
- Executes only through the configured Deriv MT5 demo account.
- Autonomous execution begins after broker-universe discovery and hard safety checks pass.
- DEMO trade, setup, execution, and model statistics are stored separately from LIVE.

### LIVE Mode (MT5)
- Real trade execution via the configured Deriv MT5 live account.
- Requires an explicit in-app confirmation for each DEMO-to-LIVE switch.
- LIVE observes and reports model performance but cannot self-promote model changes.

---

## Telegram Commands

| Command | Description |
|---------|-------------|
| `/start` or `/dashboard` | Current autonomous-system status |
| `/markets` | Broker-verified Deriv Synthetic Indices and Gold universe |
| `/positions` | Active broker positions and management actions |
| `/learning` | Measured learning evidence and next objective |
| `/performance` | Separate DEMO and LIVE performance |
| `/settings` | Mode, autonomy, safety, and universe controls |
| `/backtest [symbol] [tf] [days]` | Causal backtest using connected Deriv MT5 history only |
| `/safety` | Risk limits and circuit breakers |
| `/model` | Champion model and governance decision |
| `/emergency` | Halt new execution and optionally close positions after confirmation |
| `/help` | Operational command guide |

---

## Tech Stack

| Component | Technology |
|-----------|-----------|
| Language | Python 3.11+ |
| Telegram | python-telegram-bot v21 |
| Scheduling | APScheduler |
| Data Analysis | pandas, numpy |
| MT5 Integration | MetaTrader5 Python package |
| Database | SQLite (aiosqlite) |
| Config | pydantic-settings, python-dotenv |
| Logging | structlog |
| Containerization | Docker, docker-compose |

---

## Security

- Secrets (Telegram token, MT5 credentials) are stored in `.env` only — never in the database
- Only admin Telegram user IDs can control the bot
- No credential changes are possible via Telegram (only non-secret settings)
- `.env` is in `.gitignore` — never committed to GitHub
- `/close_all` and mode switches require confirmation

---

## Safety Defaults

| Setting | Default | Rationale |
|---------|---------|-----------|
| `TRADING_MODE` | `paper` | No real money at risk until user opts in |
| `AUTO_TRADE` | `false` | User must explicitly enable auto-execution |
| `RISK_PER_TRADE` | `1.0%` | Conservative default |
| `MAX_DAILY_LOSS_PCT` | `5.0%` | Stop trading after 5% daily loss |
| `MAX_TRADES_PER_DAY` | `10` | Prevent overtrading |
| `SCORE_THRESHOLD` | `40%` | Configurable per user request |
| `MIN_RR_RATIO` | `3.0` | 1:3 minimum as requested |
