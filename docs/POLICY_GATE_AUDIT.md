# Trading-Policy Gate Audit

**Status:** Initial audit completed against the existing codebase on 2026-08-13.

The existing system already has valuable broker-only market discovery, closed-candle analysis, outcome storage, and preliminary walk-forward model records. However, it does **not yet satisfy** the master refactor requirements because fixed trading-policy values still block DEMO research in configuration, live execution, historical backtesting, model optimization, and Telegram controls.

| Module | Active restriction | Current effect | Required disposition |
| --- | --- | --- | --- |
| `config.py` | Fixed defaults for risk, RR, daily stops, trade count, max positions/layers, confirmation requirements, and management thresholds | Creates a static-policy baseline and documents hard caps | Replace with policy-schema defaults used only to seed experiments; do not let them block DEMO trials |
| `risk/manager.py` | Daily loss/profit stop, trade count, loss streak, max positions, score, RR, setup risk, and total open-risk checks | Directly rejects DEMO execution candidates | Preserve only operational checks; move policy checks into the selected experiment policy and record every policy-caused rejection as research telemetry |
| `backtest/engine.py` | Trade count/daily loss gates, required SMC validation, RR floor, and a hidden `min(..., 1.0)` risk cap | Prevents broad policy comparison in historical evidence | Refactor to run a supplied policy object and return both actual policy outcome and eligible counterfactual variants |
| `analysis/optimizer.py` | Fixed `0.10–5.0%` risk and `1.5–5.0R` RR search limits; model baseline clamps risk to 1% | Produces small parameter nudges, not independent challenger policies | Replace with explicit candidate policies and robust-neighborhood evaluation without inherited static limits |
| `bot/handlers.py` | `/risk`, `/rr`, `/score`, `/loss_limit`, and `/open_risk` clamps plus safety-oriented messaging | Human UI can reintroduce prohibited policy ceilings | Retire legacy policy-tuning controls in favor of research, experiments, champion, challenger, and emergency controls |
| `bot/keyboards.py` and `main.py` | Legacy settings/safety/backtest menu surface | UI does not expose the research lifecycle | Replace with Dashboard, Markets, Learning, Experiments, Champion, Challengers, Performance, Research, Settings, Emergency Stop, and explicit DEMO/LIVE controls |

## Safeguards That Remain Non-Negotiable

The following are software-integrity constraints rather than preselected trading strategy. They remain active in every policy and every mode: explicit DEMO/LIVE separation, manual LIVE activation, auto-trade pause and emergency stop, broker symbol allow-list, broker volume-step/minimum/maximum compliance, valid price and stop checks, stale-candle rejection, free-margin/broker-order validation, duplicate order prevention, synchronized order state, connection/API failure handling, persistence, and restart recovery.

## Correction to Earlier Status

The earlier upgrade was a **partial foundation**, not the completed experiment engine described in the master prompt. In particular, the current optimizer changes only a narrow threshold/risk/RR setting set; it does not yet generate, independently backtest, forward-test, promote, archive, and regenerate broad policy combinations. The next implementation phases will correct that gap in the existing repository.

> The refactor will distinguish an experiment-controlled choice from a mandatory execution rule. In DEMO, a policy may choose a stop, trade count, drawdown behavior, or SMC feature combination; infrastructure failures, invalid broker instructions, and unsynchronized state may never be treated as valid experiments.

## References

This document is based solely on direct repository inspection; no external sources were used.

*This is research-system engineering, not personalized financial advice. Trading carries risk and historical or DEMO results do not guarantee LIVE results.*
