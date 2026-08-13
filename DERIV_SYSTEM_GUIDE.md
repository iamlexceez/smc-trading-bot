# Deriv SMC Learning System

## Operating purpose

This build is a **Deriv MT5-only SMC/ICT execution and learning system**. It accepts only broker-discovered **Deriv Synthetic Indices** and broker-discovered **Gold** instruments. The scanner does not infer symbols from configuration, generic feeds, or generated prices. When Deriv MT5 is disconnected or exposes no eligible instruments, scanning fails closed and no trade is placed.

> **Important:** This system is an execution and research tool, not a guarantee of profitability. A hard risk ceiling reduces exposure but cannot eliminate loss, slippage, market gaps, broker execution risk, or model error.

## Execution sequence

Every potential entry is evaluated in the following causal order using closed broker candles only.

| Stage | Requirement | Can a quality score override it? |
|---|---|---:|
| 1 | Higher-timeframe context | No |
| 2 | Meaningful liquidity and a closed-candle sweep | No |
| 3 | Directional displacement after the sweep | No |
| 4 | BOS or CHOCH in the intended direction | No |
| 5 | Retracement into a valid order block, FVG, or supply/demand zone | No |
| 6 | Structural stop beyond invalidation | No |
| 7 | Market-derived opposing-liquidity target and minimum 1:3 RR | No |
| 8 | Final broker, portfolio, margin, and daily-risk checks | No |

Only after all mandatory stages pass does the bot compute a **quality rank**. The rank prioritizes valid setups when risk capacity is scarce; it is not a probability claim and cannot turn an invalid setup into a trade.

## Safety envelope

The following controls are invariant under automatic learning and model promotion.

| Control | Enforced behavior |
|---|---|
| Per-setup risk | Hard capped at **1%**; the current preferred risk starts at **0.75%**. |
| Open portfolio risk | Hard capped at **3%**. |
| Daily loss stop | Hard capped at **3%**. |
| Emergency stop | Hard capped at **4%**. |
| Profit stop | Stops new entries at the configured daily profit threshold. |
| Loss-streak breaker | Stops new entries after the configured losing streak. |
| Layers | Confirmation-only, share one basket risk budget, and never average a losing position. |
| Stops | Never widened; management may only protect or tighten a trade. |
| LIVE mode | Starts in DEMO by default; switching to LIVE requires a separate explicit confirmation. |

The emergency control immediately pauses new execution and disables autonomous execution. It then requires a second confirmation before it asks the broker to close active positions.

## Learning records and model governance

Each evaluated setup stores a timestamped causal snapshot: structural gates, quality components, price levels, market features, broker execution result, and rejection reason. Filled trades additionally retain entry, stop, target, realized broker P/L, P/L in R, maximum favorable excursion, maximum adverse excursion, and management-layer history. Rejected but structurally complete setups are evaluated counterfactually only on **subsequently closed candles**; bars that could hit both stop and target remain explicitly ambiguous rather than being assumed profitable.

DEMO and LIVE records are partitioned in the database. The optimizer operates in **DEMO only** and uses chronological training, validation, and out-of-sample windows. A challenger can change only bounded soft parameters, such as a quality floor or a preferred risk below the hard cap. It must show positive unseen evidence with acceptable drawdown before promotion. Every challenger, promotion, rejection, and rollback is stored with parameters, windows, and measured performance. LIVE mode may report performance but cannot self-promote a model.

The daily learning report is scheduled for **07:00 UTC** by default, with a model-governance cycle one hour before it. The report distinguishes observations from conclusions and identifies the next evidence threshold rather than asserting certainty.

## Telegram operating surface

The published Telegram menu is intentionally limited to monitoring and control:

| Command | Purpose |
|---|---|
| `/start` or `/dashboard` | System status, mode, broker universe, safety, daily performance, and model state. |
| `/markets` | Broker-verified active, available, and unsupported Deriv markets. |
| `/positions` | Active broker positions and recorded management actions. |
| `/learning` | Learning evidence, profile status, and next objective. |
| `/performance` | Separate DEMO and LIVE 30-day results. |
| `/settings` | DEMO/LIVE selection, autonomous execution state, safety view, and market refresh. |
| `/backtest <symbol> <tf> <days>` | Causal backtest using connected Deriv MT5 history only. |
| `/safety` | Risk limits, circuit breakers, and current safety state. |
| `/model` | Champion version, soft parameters, out-of-sample evidence, and latest decision. |
| `/emergency` | Immediately halt new execution; optionally close positions after confirmation. |

Routine scan heartbeats and generic setup spam are suppressed. Material alerts remain: a validated eligible setup, a trade execution result, a material structural management action, a broker/data safety issue, a model promotion or rollback, and an emergency event.

## Windows VPS update procedure

From **Command Prompt** in `C:\smc-trading-bot`, stop the running bot or scheduled task first, then run:

```cmd
git pull origin main
venv\Scripts\python.exe -m pip install --only-binary=:all: -r requirements.txt
venv\Scripts\python.exe tests\smoke_upgrade.py
start_bot.bat
```

The database migration runs automatically on startup. It adds mode-isolated trade records, setup and execution telemetry, symbol profiles, and model-governance tables without requiring manual SQL. Do not copy an old settings record that forces `trading_mode=live`: an unconfirmed legacy LIVE value is migrated to DEMO.

Before starting live execution, confirm that Deriv MT5 is connected and that `/markets` lists the exact active broker instruments. A missing broker universe is a deliberate fail-closed condition, not a signal to add symbols manually.

## Reference

The closed-trade reconciliation uses MetaTrader 5 position-filtered deal history. The official Python API documentation confirms that `history_deals_get` can filter deals by a position ticket, enabling broker-confirmed outcome recording rather than inferred local outcomes.[1]

[1]: https://www.mql5.com/en/docs/python_metatrader5/mt5historydealsget_py "MetaTrader 5 Python Integration — history_deals_get"
