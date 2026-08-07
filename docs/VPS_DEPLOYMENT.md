# VPS Deployment Guide

This guide covers deploying the SMC Trading Bot on a Linux VPS.

---

## Option 1: Docker (Recommended for Paper Mode)

Docker is the simplest setup. Paper mode works perfectly in Docker.
For live MT5 mode, use Option 2 (Wine) or a Windows VPS.

### Prerequisites
- A VPS with Docker and docker-compose installed
- A Telegram bot token (from @BotFather)
- Your Telegram user ID (from @userinfobot)

### Steps

```bash
# 1. Clone the repo
git clone https://github.com/YOUR_USERNAME/smc-trading-bot.git
cd smc-trading-bot

# 2. Copy and edit environment file
cp .env.example .env
nano .env
# Fill in:
#   TELEGRAM_BOT_TOKEN=your_token
#   TELEGRAM_ADMIN_IDS=your_user_id
#   TRADING_MODE=paper

# 3. Build and start
docker-compose up -d --build

# 4. Check logs
docker-compose logs -f

# 5. Stop
docker-compose down
```

### Updating

```bash
git pull
docker-compose up -d --build
```

---

## Option 2: Linux VPS with Wine (For MT5 Live Mode)

MetaTrader5 Python package requires the MT5 terminal, which is a Windows application.
On Linux, you need Wine + Xvfb to run it.

### Prerequisites
- Ubuntu 20.04+ or Debian 11+ VPS
- 2GB+ RAM (4GB recommended)
- Root or sudo access

### Step 1: Install System Dependencies

```bash
sudo apt update && sudo apt upgrade -y
sudo apt install -y python3.11 python3.11-venv python3-pip git wine64 xvfb

# Verify Wine
wine64 --version
```

### Step 2: Install MT5 Terminal

```bash
# Download MT5 installer
wget https://download.mql5.com/cdn/web/metaquotes/mt5/x64/metaquotes5_x64.exe

# Install with Wine
wine64 metaquotes5_x64.exe

# Start virtual display (required for MT5)
Xvfb :0 -screen 0 1024x768x16 &
export DISPLAY=:0

# Launch MT5 and log in to your broker account
wine64 "~/.wine/drive_c/Program Files/MetaTrader 5/terminal64.exe"
```

### Step 3: Set Up the Bot

```bash
# Clone
git clone https://github.com/YOUR_USERNAME/smc-trading-bot.git
cd smc-trading-bot

# Create virtual environment
python3.11 -m venv venv
source venv/bin/activate

# Install dependencies
pip install -r requirements.txt

# Configure
cp .env.example .env
nano .env
# Fill in all values, set TRADING_MODE=live and MT5 credentials
# Set MT5_PATH to your Wine MT5 terminal path, e.g.:
#   MT5_PATH=/home/user/.wine/drive_c/Program Files/MetaTrader 5/terminal64.exe

# Initialize database
python -c "import asyncio; from storage import db; asyncio.run(db.init_db())"

# Start with virtual display
export DISPLAY=:0
python main.py
```

### Step 4: Run as a Service (systemd)

Create `/etc/systemd/system/smc-bot.service`:

```ini
[Unit]
Description=SMC Trading Bot
After=network.target

[Service]
Type=simple
User=your_username
WorkingDirectory=/home/your_username/smc-trading-bot
Environment=DISPLAY=:0
EnvironmentFile=/home/your_username/smc-trading-bot/.env
ExecStart=/home/your_username/smc-trading-bot/venv/bin/python main.py
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
```

```bash
# Start Xvfb as a service too
sudo tee /etc/systemd/system/xvfb.service << 'EOF'
[Unit]
Description=X Virtual Framebuffer
After=network.target

[Service]
Type=simple
ExecStart=/usr/bin/Xvfb :0 -screen 0 1024x768x16
Restart=always

[Install]
WantedBy=multi-user.target
EOF

sudo systemctl daemon-reload
sudo systemctl enable xvfb smc-bot
sudo systemctl start xvfb
sudo systemctl start smc-bot

# Check status
sudo systemctl status smc-bot
sudo journalctl -u smc-bot -f
```

---

## Option 3: Windows VPS (Simplest for MT5)

If you have a Windows VPS:

1. Install Python 3.11+ from python.org
2. Install MetaTrader 5 from your broker
3. Clone the repo: `git clone https://github.com/YOUR_USERNAME/smc-trading-bot.git`
4. `cd smc-trading-bot`
5. `pip install -r requirements.txt`
6. Copy `.env.example` to `.env` and fill in details
7. Set `MT5_PATH` to your `terminal64.exe` path
8. Run: `python main.py`
9. For auto-start, create a Windows Task Scheduler entry

---

## Recommended VPS Providers

| Provider | Min Plan | Notes |
|----------|----------|-------|
| Contabo | $6/mo | 4GB RAM, good for Wine+MT5 |
| Hetzner | $5/mo | CX11, 2GB RAM (paper mode only) |
| DigitalOcean | $6/mo | Basic droplet |
| Vultr | $6/mo | Windows VPS available |
| AWS Lightsail | $5/mo | Linux or Windows |

For **paper mode**: Any 1GB RAM Linux VPS works.
For **MT5 live mode**: 2GB+ RAM Linux (Wine) or Windows VPS.

---

## Environment Variables Reference

| Variable | Required | Default | Description |
|----------|----------|---------|-------------|
| `TELEGRAM_BOT_TOKEN` | ✅ | — | Bot token from @BotFather |
| `TELEGRAM_ADMIN_IDS` | ✅ | — | Comma-separated Telegram user IDs |
| `MT5_LOGIN` | For live | — | MT5 account number |
| `MT5_PASSWORD` | For live | — | MT5 password |
| `MT5_SERVER` | For live | — | MT5 server name |
| `MT5_PATH` | Optional | — | Path to terminal64.exe |
| `TRADING_MODE` | ❌ | `paper` | `paper` or `live` |
| `AUTO_TRADE` | ❌ | `false` | Enable auto-execution |
| `RISK_PER_TRADE` | ❌ | `1.0` | Risk % per trade |
| `MAX_DAILY_LOSS_PCT` | ❌ | `5.0` | Max daily loss % |
| `MAX_TRADES_PER_DAY` | ❌ | `10` | Max trades per day |
| `MAX_OPEN_POSITIONS` | ❌ | `5` | Max concurrent positions |
| `MIN_RR_RATIO` | ❌ | `3.0` | Minimum risk-reward |
| `SCORE_THRESHOLD` | ❌ | `40.0` | Min score to execute (%) |
| `MAX_SPREAD_PIPS` | ❌ | `5.0` | Max allowed spread |
| `SYMBOL_COOLDOWN_MINUTES` | ❌ | `30` | Cooldown after trade |
| `SYMBOLS` | ❌ | 7 symbols | Comma-separated instruments |
| `TIMEFRAMES` | ❌ | `M15,H1,H4` | Analysis timeframes |
| `DB_PATH` | ❌ | `smc_bot.db` | SQLite database path |

---

## Troubleshooting

### "MT5 not available" on Linux
This means the MetaTrader5 Python package can't connect to the terminal. Ensure:
1. Wine is installed: `wine64 --version`
2. Xvfb is running: `ps aux | grep Xvfb`
3. `DISPLAY=:0` is set
4. MT5 terminal is running: `wine64 taskmgr` (check for terminal64.exe)
5. `MT5_PATH` points to the correct terminal64.exe

### Bot not responding
1. Check the token: `curl https://api.telegram.org/bot<YOUR_TOKEN>/getMe`
2. Ensure your user ID is in `TELEGRAM_ADMIN_IDS`
3. Check logs: `docker-compose logs -f` or `journalctl -u smc-bot -f`

### Database locked
If you see SQLite database locked errors, ensure only one instance is running:
```bash
ps aux | grep main.py  # Should show one process
```

### Paper mode prices are random
Paper mode generates synthetic price data for testing. This is expected behavior.
Connect MT5 for real market data.
