# Windows VPS Setup — Phone-Friendly Guide

Since you'll be doing this from your phone, this guide is optimized for minimal typing. Every step is designed to be as painless as possible.

---

## What You Need

1. **A Windows VPS** (Windows Server 2019+ or Windows 10/11)
2. **An RDP app on your phone** (to control the VPS)
3. **A Telegram bot token** (from @BotFather)
4. **Your Telegram user ID** (from @userinfobot)
5. **MT5 installed on the VPS** with your broker account logged in

---

## Step 1: Install an RDP App on Your Phone

### Android:
- Download **Microsoft Remote Desktop** from Play Store (free)

### iPhone:
- Download **Microsoft Remote Desktop** from App Store (free)

### Connect to your VPS:
1. Open the app
2. Tap **+** → **Desktop**
3. Enter your VPS IP address
4. Enter your VPS username (usually `Administrator`) and password
5. Connect — you'll see the Windows desktop on your phone

**Tip:** Pinch to zoom. Use the on-screen keyboard for typing. It's clunky but workable.

---

## Step 2: Install Python and Git (One Time)

On the VPS desktop (via RDP from your phone):

### Install Python:
1. Open **Microsoft Edge** (or Chrome) on the VPS
2. Go to: **https://www.python.org/downloads/**
3. Click **Download Python 3.11** (or latest)
4. Run the installer
5. **CRITICAL:** Check the box **"Add Python to PATH"** at the bottom of the installer
6. Click **Install Now**
7. Wait for it to finish

### Install Git:
1. In the browser, go to: **https://git-scm.com/download/win**
2. Download and run the installer
3. Use all default settings — just keep clicking **Next** until done

### Install MT5 (if not already):
1. Download from your broker's website (e.g., Deriv, Exness, etc.)
2. Install and log in with your trading account
3. Keep MT5 running in the background

---

## Step 3: Download and Set Up the Bot

On the VPS:

1. Right-click on the desktop → **New** → **Folder** → Name it `bot`
2. Open the folder
3. In the address bar at the top, type `cmd` and press Enter
   (This opens Command Prompt in that folder)
4. Paste this command and press Enter:

```
git clone https://github.com/iamlexceez/smc-trading-bot.git
```

5. Once cloned, double-click the `smc-trading-bot` folder
6. Double-click **`setup.bat`**

This script will:
- Create a Python virtual environment
- Install all dependencies (including MetaTrader5)
- Create a `.env` configuration file
- Open the `.env` file in Notepad for you to edit

---

## Step 4: Configure the Bot

The `.env` file will open in Notepad. Fill in these values:

### Required:
```
TELEGRAM_BOT_TOKEN=1234567890:ABCdefGHIjklMNOpqrSTUvwxYZ
TELEGRAM_ADMIN_IDS=123456789
```

### For Live Trading (MT5):
```
MT5_LOGIN=12345678
MT5_PASSWORD=your_mt5_password
MT5_SERVER=Deriv-Demo
TRADING_MODE=live
```

### Leave these as defaults for now:
```
AUTO_TRADE=false
RISK_PER_TRADE=1.0
MIN_RR_RATIO=3.0
SCORE_THRESHOLD=40.0
```

Save the file (Ctrl+S) and close Notepad.

### How to get your Telegram bot token:
1. Open Telegram on your phone
2. Search for **@BotFather**
3. Send `/newbot`
4. Give it a name (e.g., "My SMC Bot")
5. Give it a username (e.g., `mysmcbot_bot`)
6. Copy the token it gives you (looks like `123456:ABC-DEF1234...`)

### How to get your Telegram user ID:
1. Search for **@userinfobot** on Telegram
2. Send `/start`
3. Copy your ID number

---

## Step 5: Start the Bot

1. In the `smc-trading-bot` folder
2. Double-click **`start_bot.bat`**
3. A black console window will open showing logs
4. You should see: `🚀 SMC Trading Bot is running!`
5. Open Telegram on your phone and send `/start` to your bot

**That's it. The bot is running.**

---

## Step 6: Auto-Start on VPS Reboot (Optional but Recommended)

So the bot restarts automatically if your VPS reboots:

1. Press **Win + R** on the VPS
2. Type `shell:startup` and press Enter
3. Right-click in the folder → **New** → **Shortcut**
4. Browse to your `start_bot.bat` file
5. Click **Next** → name it "SMC Bot" → **Finish**

Now whenever the VPS boots, the bot starts automatically.

---

## Step 7: Using the Bot from Telegram

Once running, use these commands from your phone:

| What you want | Command |
|---|---|
| See the autonomous-system dashboard | `/start` or `/dashboard` |
| View broker-verified Deriv markets | `/markets` |
| Check open positions and management actions | `/positions` |
| Review learning evidence | `/learning` |
| Review separate DEMO/LIVE performance | `/performance` |
| View or change mode and safety controls | `/settings` |
| Switch to LIVE | `/settings` → Mode → LIVE → Confirm explicitly |
| Run a causal broker-history test | `/backtest <broker-symbol> <tf> <days>` |
| Halt new execution; optionally close positions | `/emergency` → Confirm close if required |

---

## Quick Reference — Minimum Viable Setup

```
1. RDP into VPS from phone
2. Install Python (check "Add to PATH")
3. Install Git
4. Install MT5, log in to broker
5. Open CMD in a folder
6. git clone https://github.com/iamlexceez/smc-trading-bot.git
7. cd smc-trading-bot
8. Double-click setup.bat
9. Edit .env with your token, ID, and MT5 details
10. Double-click start_bot.bat
11. Message your bot on Telegram: /start
```

---

## Troubleshooting

### "Python is not recognized"
You didn't check "Add Python to PATH" during installation.
Reinstall Python and check that box.

### "git is not recognized"
Git wasn't installed. Download from git-scm.com/download/win

### Bot starts but doesn't respond
- Check your bot token is correct in `.env`
- Check your admin ID is correct (no spaces)
- Make sure you're messaging the right bot on Telegram

### "MT5 not available"
- Make sure MT5 is installed and running on the VPS
- Make sure you entered your MT5 login, password, and server in `.env`
- The bot falls back to paper mode if MT5 isn't available (this is fine for testing)

### VPS disconnected and bot stopped
- Reconnect via RDP
- Double-click `start_bot.bat` again
- Set up auto-start (Step 6) so it restarts automatically

### How to update the bot
1. Open CMD in the `smc-trading-bot` folder
2. Run: `git pull`
3. Run: `start_bot.bat`
