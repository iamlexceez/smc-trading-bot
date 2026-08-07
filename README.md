# 🤖 SMC Trading Bot

A Telegram-controlled trading bot that uses **APA (Advanced Price Action)** and **Supply & Demand** strategies to analyze currency pairs, synthetic indices, and gold. It scores each setup using a 7-factor system, and auto-executes trades that pass a configurable threshold with a minimum 1:3 risk-reward ratio.

## ⚠️ Disclaimer

This is a configurable rule engine, not a guaranteed profitable system. Trading involves substantial risk of loss. Always test in paper mode first. The authors are not responsible for any financial losses.

---

## Features

- **APA Market Structure**: Swing detection, BOS/CHoCH, order blocks, FVGs, liquidity pools, premium/discount zones
- **Supply & Demand Zones**: Rally-Base-Drop, Drop-Base-Rally patterns with freshness and strength scoring
- **7-Factor Scoring Engine**: Structure alignment, S/D zones, order blocks, FVG, liquidity sweep, RR ratio, MTF confluence
- **Auto-Execution**: Trades that pass the score threshold AND all risk gates execute automatically
- **Risk Management**: Max daily loss, max trades/day, max positions, symbol cooldown, spread checks, margin checks
- **Telegram Control**: Full settings adjustment, position management, analysis viewing — all from Telegram
- **Paper & Live Modes**: Test safely in paper mode, switch to live MT5 execution when ready
- **VPS Ready**: Docker support, systemd service files, Wine+MT5 guide

## Quick Start

```bash
# 1. Clone
git clone https://github.com/YOUR_USERNAME/smc-trading-bot.git
cd smc-trading-bot

# 2. Configure
cp .env.example .env
# Edit .env with your Telegram bot token and admin ID

# 3. Install
pip install -r requirements.txt

# 4. Run (paper mode by default)
python main.py
```

## Telegram Setup

1. Create a bot: Message [@BotFather](https://t.me/BotFather) on Telegram
2. Send `/newbot` and follow the prompts
3. Copy the bot token to `.env` as `TELEGRAM_BOT_TOKEN`
4. Message [@userinfobot](https://t.me/userinfobot) to get your user ID
5. Add your ID to `TELEGRAM_ADMIN_IDS` in `.env`

## Commands

| Command | Description |
|---------|-------------|
| `/start` | Main menu |
| `/scan` | Scan all symbols for signals |
| `/analyze [symbol]` | Deep analysis of a symbol |
| `/positions` | Open positions |
| `/close_all` | Close all positions |
| `/settings` | Adjust settings via inline buttons |
| `/account` | Account info |
| `/history` | Trade history |
| `/pause` / `/resume` | Pause/resume auto-trading |
| `/mode [paper\|live]` | Switch execution mode |
| `/risk [pct]` | Set risk per trade |
| `/rr [ratio]` | Set min RR ratio |
| `/score [val]` | Set score threshold |
| `/help` | Show all commands |

## Scoring System

| Factor | Weight |
|--------|--------|
| Market Structure Alignment | 20% |
| Supply/Demand Zone | 15% |
| Order Block | 15% |
| Fair Value Gap | 10% |
| Liquidity Sweep | 15% |
| Risk-Reward Ratio | 15% |
| MTF Confluence | 10% |

A trade executes when score ≥ threshold (default 40%) AND all risk gates pass.

## Supported Instruments

- **Forex**: EURUSD, GBPUSD, USDJPY, etc.
- **Gold**: XAUUSD
- **Synthetic Indices**: Volatility 75, Boom 500, Crash 500 (via Deriv MT5)

## Documentation

- [Full Specification](docs/SPEC.md) — Features, scoring details, architecture
- [VPS Deployment Guide](docs/VPS_DEPLOYMENT.md) — Docker, Wine+MT5, Windows VPS

## Tech Stack

Python 3.11 · python-telegram-bot · APScheduler · pandas · MetaTrader5 · SQLite · Docker

## License

This project is for educational purposes. Use at your own risk.
