# Windows VPS Deployment Guide

The Deriv-only learning system runs against a **connected Deriv MT5 terminal on Windows**. Docker, generic paper-mode backends, unrelated market-data feeds, and generated-price fallbacks are unsupported because the system requires broker-native symbol discovery, closed candles, execution details, and position-deal history.

Use the current operational guide first: [`DERIV_SYSTEM_GUIDE.md`](../DERIV_SYSTEM_GUIDE.md).

## Supported update procedure

Open **Command Prompt** on the VPS and run the following from `C:\smc-trading-bot` after stopping the running process or scheduled task.

```cmd
git pull origin main
venv\Scripts\python.exe -m pip install --only-binary=:all: -r requirements.txt
venv\Scripts\python.exe tests\smoke_upgrade.py
start_bot.bat
```

The database migration runs on startup. It preserves prior data while adding DEMO/LIVE partitioning, setup telemetry, execution records, symbol profiles, and model-governance tables.

## Pre-start checks

Confirm that Deriv MT5 is open and logged in to the intended account. Start in **DEMO** mode, then use `/markets` to verify that the bot has discovered active Deriv Synthetic Indices and/or broker-listed Gold. If no eligible broker instruments appear, the system intentionally does not scan or trade.

Switching to LIVE requires a separate explicit Telegram confirmation. `/emergency` immediately halts new execution and only closes existing positions after an additional confirmation.
