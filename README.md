# SMC Trading Bot

A Python and MetaTrader 5 trading system for **causal Smart Money Concepts (SMC) analysis**, Telegram control, and risk-capped execution. The live scanner, trade manager, and backtest engine share the same design principle: **a setup must be valid before it is scored, and a score cannot override a hard risk gate**.

> **Important:** This software is an execution and analysis tool, not a guarantee of profitability. Automated trading can lose capital quickly. Keep `AUTO_TRADE=false` while testing in demo mode and enable live execution only after you have independently reviewed its behaviour.

## Causal Setup Pipeline

The bot evaluates completed candles only. It does not manufacture a stop or target to achieve a desired reward-to-risk ratio, and it does not allow a setup-quality score to relax required conditions.

| Stage | Required condition | Effect if absent |
|---|---|---|
| 1. Context | Higher-timeframe trend alignment or a non-conflicting confirmed reversal | No setup |
| 2. Liquidity | A known buy-side or sell-side pool | No setup |
| 3. Sweep | Closed-candle sweep and reclaim of that known pool | No setup |
| 4. Displacement | Directional body and range expansion against ATR | No setup |
| 5. Structure | Candle-close BOS or CHOCH in the intended direction | No setup |
| 6. Location | Retracement into a fresh directional order block, FVG, or supply/demand zone | No setup |
| 7. Risk and target | Structural stop beyond invalidation and opposing unswept liquidity target | No setup |
| 8. Quality | Transparent quality ranking after all hard checks pass | Ranking only; no risk increase |

## Entry Modes

| Mode | Additional condition | Intended use |
|---|---|---|
| `confirmed` | Lower-timeframe confirmation after the complete hard-gate sequence | Most selective mode |
| `aggressive` | May enter before a separate candle pattern, but still requires every hard structural, liquidity, zone, risk, and target condition | Earlier but still gated execution |
| `extreme` | Explicit opt-in, all hard gates, quality score of at least 90, and positive historical expectancy when available | Exceptional situations only |

The selected entry mode changes **timing**, not the maximum financial risk. `extreme` is disabled by default.

## Risk and Basket Controls

A setup receives one risk budget. Layers are portions of that original budget and are not a mechanism for averaging into a losing position.

| Control | Default | Behaviour |
|---|---:|---|
| Risk per setup | 0.75% | User-adjustable but hard-capped at 1% |
| Setup-risk cap | 1.00% | Never increased by confidence, score, or aggressive mode |
| Total open-risk ceiling | 3.00% | Blocks new execution when protected downside would exceed it |
| Daily loss stop | 3.00% | Halts new trading for the day |
| Emergency loss stop | 4.00% | Independent final loss circuit breaker |
| Daily profit stop | 10.00% | Halts new trading after a successful day |
| Consecutive-loss breaker | 3 losses | Blocks new entries until the streak resets |
| Max simultaneous setups | 2 | Limits concurrent independent exposure |
| Max layers per setup | 4 | Uses `40% / 30% / 20% / 10%` planned volume by default |

Position sizing uses the broker’s tick size, tick value, minimum volume, maximum volume, and volume step. It chooses the lower of the risk-limited and margin-limited volume, then rounds **down** to the broker’s valid step.

## Autonomous Position Management

The bot records each bot-managed setup as a persistent basket, including its initial structural stop, fixed risk budget, planned layers, and management actions. Active management works from fresh M5 closed-candle analysis:

- It never widens a stop loss.
- It moves to breakeven only after one R **and** fresh directional structure confirmation.
- It can lock partial profit at 1.5R and one R at 2R when structure confirms.
- It trails behind confirmed protected swings rather than a blind fixed-distance rule.
- It may partially realize 20% at runner conditions, when enabled.
- It may extend a target only to a new, favourable liquidity target that preserves the minimum R:R.
- It exits an automated position when a fresh opposing structural event invalidates the thesis.

Manual MT5 positions remain visible in `/positions`. They can be reviewed defensively, but the bot does not create automated layers for positions without a recorded basket.

## Telegram Controls

| Command | Description |
|---|---|
| `/scan` | Run the causal market scanner; only valid, quality-qualified setups are considered for execution |
| `/positions` | Show live positions, recorded bot actions, and basket status |
| `/baskets` | Show risk budgets, active layers, and planned layers |
| `/manage [ticket]` | Re-analyse one position and make only a safe SL/TP improvement |
| `/safety` | Show risk caps, daily stops, open-risk ceiling, and loss-streak status |
| `/entry_mode [confirmed\|aggressive\|extreme confirm]` | Select the setup timing model |
| `/risk [pct]` | Set risk per setup within the hard one-percent cap |
| `/rr [ratio]` | Set the minimum market-derived R:R |
| `/score [pct]` | Set the minimum post-validation quality threshold |
| `/daily_limit [pct]` | Set the daily profit stop; daily loss protection remains separate |
| `/journal` | Show the daily performance and setup-quality journal |
| `/backtest [symbol] [tf] [days]` | Run the causal backtest pipeline |

## Installation and VPS Update

On the Windows VPS, open a terminal in the bot folder and run:

```powershell
git pull origin main
py -m pip install -r requirements.txt
py -m pip install -r requirements-mt5.txt
```

Copy any desired non-secret defaults from `.env.example` into the VPS `.env`. Do **not** replace existing credentials with the example values. For a conservative first restart, use:

```env
AUTO_TRADE=false
ENTRY_MODE=confirmed
RISK_PER_TRADE=0.75
MAX_SETUP_RISK_PCT=1.0
MAX_TOTAL_OPEN_RISK_PCT=3.0
MAX_DAILY_LOSS_PCT=3.0
DAILY_PROFIT_STOP_PCT=10.0
MIN_SETUP_SCORE=75.0
```

Restart with the existing `start_bot.bat`, then use `/debug_mt5`, `/safety`, and `/scan` before enabling automatic execution.

## Verification

The repository includes deterministic local verification at `tests/smoke_upgrade.py`. It tests configuration round trips, risk caps, step-floor sizing, loss exposure after a profit-protected stop, no-widening stop behaviour, causal swing confirmation, displacement detection, basket persistence, and module imports. It does not connect to MT5 or place, modify, or close trades.

```powershell
py tests\smoke_upgrade.py
```

## Technology

Python 3.12, MetaTrader5, python-telegram-bot, APScheduler, pandas, NumPy, matplotlib, aiosqlite, and aiohttp.
