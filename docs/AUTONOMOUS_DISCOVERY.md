# SMC Trading Bot — Institutional Autonomous Research & Policy Discovery Architecture

## Executive Overview
The SMC Trading Bot has been successfully refactored from a static-rule MT5 script into an **institutional-grade, autonomous research and execution system** built exclusively for Deriv Synthetic Indices and Gold (`XAUUSD`, `XAUUSDmicro`). 

In strict compliance with architectural requirements, all trading policies—including position sizing, risk percentage, risk-to-reward ratios, drawdown limits, stop-loss/take-profit methodology, layering, and trade frequency—are no longer hard-coded. Instead, they are treated as **experimental variables** managed by a walk-forward champion/challenger optimization engine (`analysis/optimizer.py`) running in DEMO mode.

---

## 1. Core Architectural Pillars

### A. Broker-Verified Discovery & Causal Data Layer (`data/universe.py`, `data/provider.py`)
- **Strict Instrument Scope**: Only Deriv Synthetic Indices and Gold (`XAUUSD`, `XAUUSDmicro`) are permitted. All forex, crypto, and external market dependencies are permanently purged.
- **Closed-Candle Causal Analysis**: Eliminates look-ahead bias by exclusively consuming broker-verified closed candles for all statistical profiling, structure detection, and backtesting.

### B. Variable-Driven Policy Engine (`config.py`, `risk/manager.py`, `execution/manager.py`)
- **No Hard-Coded Rules**: Risk percentages, setup filters, reward-to-risk ratios, entry modes, and exposure caps are dynamically adjusted by the optimizer.
- **Software Integrity vs. Trading Policy**: While trading policy is completely experimental, core technical safeguards remain strictly enforced:
  - Duplicate-order prevention and atomic order tracking.
  - Invalid-order protection and broker volume-step flooring (`RiskManager.floor_volume`).
  - Stale-data detection, crash recovery, and automated MT5 IPC session healing.
  - Emergency shutdown (`/stop`) and deliberate balance exhaustion/testing (`/burn_to`).

### C. Walk-Forward Champion/Challenger Optimizer (`analysis/optimizer.py`)
- **Walk-Forward Windows**: Continuously evaluates rolling training, validation, and out-of-sample periods.
- **Evolutionary Learning**: Pits challenger parameter sets against active champions. Superior parameter configurations (optimizing expectancy $R$, win rate, profit factor, and max drawdown $R$) are automatically promoted.
- **Immersive Daily Journal**: Generates a comprehensive daily journal documenting what the bot learned from market price action, volatility regimes, and self-execution metrics.

---

## 2. Experimental Variable Spectrum

The autonomous research engine freely tests and optimizes the following variables:
1. **Risk per Trade** & **Max Setup Risk %**
2. **Reward-to-Risk (RR) Ratios**
3. **Setup Quality Score Thresholds** & **Scoring Weights**
4. **SL/TP Placement Methodologies** (ATR buffer, structure pivots, FVG midpoints)
5. **Layering Allocations & Limits** (non-martingale scaling across valid pullbacks)
6. **Trade Frequency** & **Symbol Cooldowns** (aggressive scaling in DEMO to accelerate learning)
7. **Daily PnL Targets** & **Drawdown Stop Triggers**

---

## 3. Operational Modes & Commands

- **DEMO Mode**: The default autonomous environment where the bot experiments freely, executes aggressive cycles, records counterfactual telemetry for rejected setups, and evolves its trading policy.
- **LIVE Mode**: Strictly gated behind explicit user confirmation. Live trading will never activate automatically.
- **Key Telegram Commands**:
  - `/dashboard`: Real-time monitoring of active champion policy, equity, margin, and open positions.
  - `/markets`: Verified Deriv Synthetic Indices and Gold universe status.
  - `/learning`: Active champion vs. challenger stats and walk-forward evolution history.
  - `/journal`: Immersive trader's daily journal explaining chart discoveries and optimization shifts.
  - `/burn_to [target]`: Deliberate demo balance exhaustion/testing tool.
  - `/toggle`: Switch auto-trade or aggressive growth modes.
