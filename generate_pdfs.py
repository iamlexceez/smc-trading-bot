"""Generate PDF documents for the SMC Trading Bot project."""

from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.units import inch, mm
from reportlab.platypus import (
    SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle,
    PageBreak, ListFlowable, ListItem
)
from reportlab.lib.enums import TA_LEFT, TA_CENTER
import os

# Colors from design-foundations palette
BG = colors.HexColor("#F7F6F2")
TEXT = colors.HexColor("#28251D")
MUTED = colors.HexColor("#7A7974")
PRIMARY = colors.HexColor("#01696F")
BORDER = colors.HexColor("#D4D1CA")
SUCCESS = colors.HexColor("#437A22")
WARNING = colors.HexColor("#964219")
ERROR = colors.HexColor("#A12C7B")

OUTPUT_DIR = "/home/user/workspace/smc-trading-bot/docs"

styles = getSampleStyleSheet()

# Custom styles
title_style = ParagraphStyle("CustomTitle", parent=styles["Title"], fontSize=28, textColor=PRIMARY, spaceAfter=6, alignment=TA_LEFT)
subtitle_style = ParagraphStyle("Subtitle", parent=styles["Normal"], fontSize=14, textColor=MUTED, spaceAfter=20, alignment=TA_LEFT)
h1_style = ParagraphStyle("H1", parent=styles["Heading1"], fontSize=20, textColor=PRIMARY, spaceBefore=24, spaceAfter=12)
h2_style = ParagraphStyle("H2", parent=styles["Heading2"], fontSize=16, textColor=TEXT, spaceBefore=16, spaceAfter=8)
h3_style = ParagraphStyle("H3", parent=styles["Heading3"], fontSize=13, textColor=PRIMARY, spaceBefore=12, spaceAfter=6)
body_style = ParagraphStyle("Body", parent=styles["Normal"], fontSize=10, textColor=TEXT, spaceAfter=6, leading=15)
code_style = ParagraphStyle("Code", parent=styles["Code"], fontSize=9, textColor=TEXT, backColor=colors.HexColor("#EFEEEA"), leftIndent=12, rightIndent=12, spaceAfter=8, spaceBefore=4, borderColor=BORDER, borderWidth=1, borderPadding=6)
note_style = ParagraphStyle("Note", parent=styles["Normal"], fontSize=10, textColor=WARNING, spaceAfter=8, leftIndent=12, italic=True)
bold_body = ParagraphStyle("BoldBody", parent=body_style, fontName="Helvetica-Bold")


def make_table(data, col_widths=None):
    """Create a styled table."""
    t = Table(data, colWidths=col_widths)
    t.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), PRIMARY),
        ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
        ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
        ("FONTSIZE", (0, 0), (-1, -1), 9),
        ("FONTNAME", (0, 1), (-1, -1), "Helvetica"),
        ("TEXTCOLOR", (0, 1), (-1, -1), TEXT),
        ("BACKGROUND", (0, 1), (-1, -1), colors.HexColor("#F9F8F5")),
        ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.HexColor("#F9F8F5"), colors.HexColor("#FBFBF9")]),
        ("GRID", (0, 0), (-1, -1), 0.5, BORDER),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("LEFTPADDING", (0, 0), (-1, -1), 6),
        ("TOPPADDING", (0, 0), (-1, -1), 5),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
    ]))
    return t


def generate_spec_pdf():
    """Generate the specification PDF."""
    doc = SimpleDocTemplate(
        os.path.join(OUTPUT_DIR, "SMC_Trading_Bot_Specification.pdf"),
        pagesize=A4,
        rightMargin=50, leftMargin=50, topMargin=50, bottomMargin=50,
        title="SMC Trading Bot — Specification",
        author="Perplexity Computer",
    )

    story = []

    # Title
    story.append(Paragraph("SMC Trading Bot", title_style))
    story.append(Paragraph("Complete Specification Document", subtitle_style))
    story.append(Paragraph("A Telegram-controlled trading bot using APA (Advanced Price Action) and Supply & Demand strategies with multi-factor scoring, backtesting, and auto-execution.", body_style))
    story.append(Spacer(1, 12))
    story.append(Paragraph("⚠️ This is a configurable rule engine, not a guaranteed profitable system. Trading involves substantial risk of loss. Always test in paper mode first.", note_style))

    # Overview
    story.append(Paragraph("Overview", h1_style))
    story.append(Paragraph("The bot analyzes currency pairs, synthetic indices, and gold using professional APA/SMC concepts. It scores each setup using a 7-factor system, and auto-executes trades that pass a configurable threshold with a minimum 1:3 risk-reward ratio. Includes full backtesting, entry confirmation, trade management, session filtering, and news filtering.", body_style))

    # Architecture
    story.append(Paragraph("Architecture", h1_style))
    arch_data = [
        ["Module", "Description"],
        ["analysis/structure.py", "APA market structure: BOS, CHoCH, OB, FVG, liquidity pools"],
        ["analysis/supply_demand.py", "S/D zone detection with freshness and strength scoring"],
        ["analysis/scoring.py", "7-factor signal scoring engine"],
        ["analysis/confirmation.py", "Entry confirmation: zone retest + candle patterns"],
        ["analysis/sessions.py", "Session filtering (London, NY, Tokyo)"],
        ["data/provider.py", "Real market data: MT5 → Twelve Data → yfinance → synthetic"],
        ["executors/paper.py", "Paper trading simulator (default)"],
        ["executors/mt5.py", "MetaTrader 5 live execution"],
        ["execution/manager.py", "Trade management: breakeven, trailing, partial close"],
        ["risk/manager.py", "10 hard risk gates + position sizing"],
        ["news/filter.py", "Economic calendar news filter (ForexFactory)"],
        ["backtest/engine.py", "Full backtesting engine with metrics"],
        ["backtest/runner.py", "CLI + Telegram backtest runner"],
        ["bot/handlers.py", "Telegram command handlers"],
        ["storage/db.py", "SQLite persistence"],
        ["scheduler.py", "Market scanner + auto-execution loop"],
    ]
    story.append(make_table(arch_data, col_widths=[180, 320]))

    # APA Analysis
    story.append(PageBreak())
    story.append(Paragraph("APA (Advanced Price Action) Analysis", h1_style))
    apa_data = [
        ["Component", "Description"],
        ["Swing Detection", "Identifies swing highs/lows using configurable lookback (default 3 bars)"],
        ["Market Structure", "Bullish (HH+HL), Bearish (LH+LL), or Ranging"],
        ["BOS", "Break of Structure — trend continuation signal"],
        ["CHoCH", "Change of Character — trend reversal signal (tracks prior trend from 3rd swing)"],
        ["Order Blocks", "Last opposite candle before institutional impulse move"],
        ["Fair Value Gaps", "3-candle imbalances where price hasn't returned"],
        ["Liquidity Pools", "Equal highs (buy-side) and equal lows (sell-side) where stops accumulate"],
        ["Premium/Discount", "Equilibrium-based zone classification"],
    ]
    story.append(make_table(apa_data, col_widths=[140, 360]))

    # S/D Zones
    story.append(Paragraph("Supply & Demand Zones", h2_style))
    sd_data = [
        ["Pattern", "Type", "Description"],
        ["Rally-Base-Drop", "Supply", "Up → consolidation → sharp drop"],
        ["Drop-Base-Drop", "Supply", "Down → consolidation → sharp drop"],
        ["Drop-Base-Rally", "Demand", "Down → consolidation → sharp rally"],
        ["Rally-Base-Rally", "Demand", "Up → consolidation → sharp rally"],
    ]
    story.append(make_table(sd_data, col_widths=[140, 80, 280]))
    story.append(Spacer(1, 8))
    story.append(Paragraph("Each zone is scored on impulse strength (40%), freshness (40%), and base tightness (20%). Fresh zones have not been revisited by price since creation.", body_style))

    # Scoring
    story.append(PageBreak())
    story.append(Paragraph("7-Factor Scoring System", h1_style))
    score_data = [
        ["#", "Factor", "Weight", "Max"],
        ["1", "Market Structure Alignment", "20%", "20"],
        ["2", "Supply/Demand Zone", "15%", "15"],
        ["3", "Order Block Confluence", "15%", "15"],
        ["4", "Fair Value Gap", "10%", "10"],
        ["5", "Liquidity Sweep", "15%", "15"],
        ["6", "Risk-Reward Ratio", "15%", "15"],
        ["7", "MTF Confluence", "10%", "10"],
        ["", "Total", "100%", "100"],
    ]
    story.append(make_table(score_data, col_widths=[30, 250, 100, 60]))

    story.append(Paragraph("Entry Confirmation", h2_style))
    story.append(Paragraph("Before executing, the bot verifies entry confirmation (configurable):", body_style))
    conf_data = [
        ["Confirmation", "Description"],
        ["Zone Retest", "Price must have touched the S/D zone within recent bars"],
        ["Engulfing", "Bullish/bearish engulfing candle pattern"],
        ["Pin Bar", "Hammer/shooting star with long wick"],
        ["Inside Bar Breakout", "Mother candle → inside bar → breakout in trade direction"],
        ["Displacement", "Strong institutional impulse (body > 2×ATR)"],
    ]
    story.append(make_table(conf_data, col_widths=[140, 360]))

    # Risk Management
    story.append(PageBreak())
    story.append(Paragraph("Risk Management — 10 Hard Gates", h1_style))
    risk_data = [
        ["Gate", "Description", "Default"],
        ["Auto-trade enabled", "auto_trade=true and not paused", "OFF"],
        ["Symbol allowed", "Symbol is in configured list", "—"],
        ["Symbol cooldown", "No trade within cooldown period", "30 min"],
        ["Daily loss limit", "PnL > -(balance × max_loss%)", "5%"],
        ["Daily trade count", "Trades today < max", "10"],
        ["Max positions", "Open positions < max", "5"],
        ["Score threshold", "Score ≥ threshold", "40%"],
        ["Min RR ratio", "RR ≥ min_rr_ratio", "1:3"],
        ["Spread check", "Spread ≤ max_spread_pips", "5 pips"],
        ["Free margin", "Free margin > 2× required", "—"],
    ]
    story.append(make_table(risk_data, col_widths=[130, 270, 60]))

    # Trade Management
    story.append(Paragraph("Trade Management", h2_style))
    story.append(Paragraph("Once a trade is open, the bot actively manages it:", body_style))
    tm_data = [
        ["Feature", "Description", "Default"],
        ["Breakeven", "Move SL to entry at 1R profit", "1.0R"],
        ["Partial Close", "Close 50% at 2R, trail the rest", "2.0R / 50%"],
        ["Trailing Stop", "ATR-based trailing behind price", "2×ATR"],
        ["Time Exit", "Close if held >100 bars without 1R", "100 bars"],
    ]
    story.append(make_table(tm_data, col_widths=[110, 290, 80]))

    # Session & News
    story.append(Paragraph("Session & News Filtering", h2_style))
    story.append(Paragraph("The bot only trades during high-liquidity sessions:", body_style))
    sess_data = [
        ["Session", "Hours (UTC)", "Default"],
        ["Tokyo", "00:00 - 09:00", "OFF"],
        ["London", "08:00 - 17:00", "ON"],
        ["New York", "13:00 - 22:00", "ON"],
        ["London/NY Overlap", "13:00 - 17:00", "ON"],
    ]
    story.append(make_table(sess_data, col_widths=[150, 200, 80]))
    story.append(Spacer(1, 8))
    story.append(Paragraph("News Filter: Uses ForexFactory economic calendar (free, no API key). Blocks trading 15 minutes before and after high-impact events for the relevant currencies.", body_style))

    # Backtesting
    story.append(PageBreak())
    story.append(Paragraph("Backtesting Engine", h1_style))
    story.append(Paragraph("Full historical replay through the analysis pipeline with realistic spread, slippage, and commission.", body_style))
    story.append(Paragraph("Run from Telegram: /backtest EURUSD H1 180", code_style))
    story.append(Paragraph("Run from CLI: python -m backtest.runner --symbol EURUSD --timeframe H1 --days 180", code_style))
    story.append(Spacer(1, 8))
    story.append(Paragraph("Metrics computed:", body_style))
    metrics_data = [
        ["Metric", "Description"],
        ["Win Rate", "Percentage of winning trades"],
        ["Profit Factor", "Gross profit / gross loss"],
        ["Expectancy", "Average P&L per trade"],
        ["Max Drawdown", "Largest peak-to-trough decline (%)"],
        ["Sharpe Ratio", "Risk-adjusted return (annualized)"],
        ["Sortino Ratio", "Downside-adjusted return"],
        ["Monthly Returns", "P&L breakdown by month"],
        ["Avg RR", "Average risk-reward achieved"],
        ["Avg Bars Held", "Average trade duration"],
    ]
    story.append(make_table(metrics_data, col_widths=[140, 360]))

    # Data Sources
    story.append(Paragraph("Real Market Data Sources", h1_style))
    story.append(Paragraph("Paper mode now uses real market data instead of synthetic random walks:", body_style))
    data_data = [
        ["Source", "Instruments", "Cost"],
        ["MetaTrader 5", "All (forex, gold, synthetic indices)", "Free (needs MT5 terminal)"],
        ["Twelve Data API", "Forex + gold (140 currencies)", "Free tier: 8 req/min, 800/day"],
        ["yfinance", "Forex + gold", "Free, no API key"],
        ["Synthetic", "Synthetic indices (fallback)", "Free (random walk)"],
    ]
    story.append(make_table(data_data, col_widths=[120, 250, 130]))

    # Commands
    story.append(PageBreak())
    story.append(Paragraph("Telegram Commands", h1_style))
    cmd_data = [
        ["Command", "Description"],
        ["/start", "Main menu with quick stats"],
        ["/scan", "Scan all symbols for signals"],
        ["/analyze [symbol]", "Deep analysis of specific symbol"],
        ["/backtest [symbol] [tf] [days]", "Run historical backtest"],
        ["/positions", "Show open positions"],
        ["/close_all", "Close all positions (with confirmation)"],
        ["/settings", "Adjust all settings via inline keyboards"],
        ["/account", "Show account info"],
        ["/history", "Recent trade history"],
        ["/sessions", "Show trading session status"],
        ["/news [symbol]", "Check news blackout status"],
        ["/pause / /resume", "Pause/resume auto-trading"],
        ["/mode [paper|live]", "Switch execution mode"],
        ["/risk [pct]", "Set risk per trade"],
        ["/rr [ratio]", "Set min RR ratio"],
        ["/score [val]", "Set score threshold"],
    ]
    story.append(make_table(cmd_data, col_widths=[180, 320]))

    # Tech Stack
    story.append(Paragraph("Tech Stack", h1_style))
    stack_data = [
        ["Component", "Technology"],
        ["Language", "Python 3.11+"],
        ["Telegram", "python-telegram-bot v21"],
        ["Scheduling", "APScheduler"],
        ["Data Analysis", "pandas, numpy"],
        ["MT5 Integration", "MetaTrader5 Python package"],
        ["Market Data", "Twelve Data API, yfinance"],
        ["Database", "SQLite (aiosqlite)"],
        ["Backtesting", "Custom engine with full metrics"],
        ["Containerization", "Docker, docker-compose"],
    ]
    story.append(make_table(stack_data, col_widths=[180, 320]))

    # Safety
    story.append(Paragraph("Safety Defaults", h1_style))
    safety_data = [
        ["Setting", "Default", "Rationale"],
        ["TRADING_MODE", "paper", "No real money at risk until user opts in"],
        ["AUTO_TRADE", "false", "User must explicitly enable"],
        ["RISK_PER_TRADE", "1.0%", "Conservative default"],
        ["MAX_DAILY_LOSS_PCT", "5.0%", "Stop trading after 5% daily loss"],
        ["SCORE_THRESHOLD", "40.0%", "Configurable threshold"],
        ["MIN_RR_RATIO", "3.0", "1:3 minimum as requested"],
        ["Entry Confirmation", "ON", "Zone retest + candle pattern required"],
        ["Session Filter", "London+NY", "Only trade high-liquidity sessions"],
        ["News Filter", "ON", "Block during high-impact news"],
    ]
    story.append(make_table(safety_data, col_widths=[150, 100, 250]))

    doc.build(story)
    print("✅ Generated SMC_Trading_Bot_Specification.pdf")


def generate_deploy_pdf():
    """Generate the Windows VPS deployment guide PDF."""
    doc = SimpleDocTemplate(
        os.path.join(OUTPUT_DIR, "Windows_VPS_Setup_Guide.pdf"),
        pagesize=A4,
        rightMargin=50, leftMargin=50, topMargin=50, bottomMargin=50,
        title="SMC Trading Bot — Windows VPS Setup Guide",
        author="Perplexity Computer",
    )

    story = []
    story.append(Paragraph("SMC Trading Bot", title_style))
    story.append(Paragraph("Windows VPS Setup Guide — Phone-Friendly", subtitle_style))

    story.append(Paragraph("This guide is optimized for setting up the bot from your phone via RDP. Minimal typing required.", body_style))

    # Prerequisites
    story.append(Paragraph("What You Need", h1_style))
    pre_data = [
        ["Item", "Details"],
        ["Windows VPS", "Windows Server 2019+ or Windows 10/11"],
        ["RDP App", "Microsoft Remote Desktop (free on iOS/Android)"],
        ["Telegram Bot Token", "From @BotFather on Telegram"],
        ["Your Telegram ID", "From @userinfobot on Telegram"],
        ["MT5 Installed", "With your broker account logged in"],
    ]
    story.append(make_table(pre_data, col_widths=[150, 350]))

    # Step 1
    story.append(Paragraph("Step 1: Install RDP App", h1_style))
    story.append(Paragraph("Download Microsoft Remote Desktop from your app store (free). Connect to your VPS using its IP address, username (usually Administrator), and password. You'll see the Windows desktop on your phone.", body_style))

    # Step 2
    story.append(Paragraph("Step 2: Install Python and Git", h1_style))
    story.append(Paragraph("Install Python:", bold_body))
    story.append(Paragraph("1. Open browser on VPS, go to python.org/downloads", body_style))
    story.append(Paragraph("2. Download Python 3.11+", body_style))
    story.append(Paragraph("3. CRITICAL: Check 'Add Python to PATH' during installation", body_style))
    story.append(Paragraph("4. Click Install Now", body_style))
    story.append(Spacer(1, 6))
    story.append(Paragraph("Install Git:", bold_body))
    story.append(Paragraph("1. Go to git-scm.com/download/win", body_style))
    story.append(Paragraph("2. Download and run installer, use all defaults", body_style))
    story.append(Spacer(1, 6))
    story.append(Paragraph("Install MT5 (if not already):", bold_body))
    story.append(Paragraph("Download from your broker, install, and log in with your trading account. Keep MT5 running in the background.", body_style))

    # Step 3
    story.append(PageBreak())
    story.append(Paragraph("Step 3: Download and Set Up the Bot", h1_style))
    story.append(Paragraph("1. Right-click desktop → New → Folder → Name it 'bot'", body_style))
    story.append(Paragraph("2. Open the folder, type 'cmd' in the address bar, press Enter", body_style))
    story.append(Paragraph("3. Paste this command:", body_style))
    story.append(Paragraph("git clone https://github.com/iamlexceez/smc-trading-bot.git", code_style))
    story.append(Paragraph("4. Open the smc-trading-bot folder", body_style))
    story.append(Paragraph("5. Double-click setup.bat — it installs everything automatically", body_style))
    story.append(Paragraph("The setup script creates a virtual environment, installs all dependencies, and creates a .env file for you to edit.", body_style))

    # Step 4
    story.append(Paragraph("Step 4: Configure the Bot", h1_style))
    story.append(Paragraph("The .env file opens in Notepad. Fill in:", body_style))
    story.append(Paragraph("TELEGRAM_BOT_TOKEN=your_token_from_botfather", code_style))
    story.append(Paragraph("TELEGRAM_ADMIN_IDS=your_id_from_userinfobot", code_style))
    story.append(Paragraph("MT5_LOGIN=your_mt5_account_number", code_style))
    story.append(Paragraph("MT5_PASSWORD=your_mt5_password", code_style))
    story.append(Paragraph("MT5_SERVER=your_broker_server", code_style))
    story.append(Paragraph("TRADING_MODE=live", code_style))
    story.append(Spacer(1, 8))
    story.append(Paragraph("How to get your Telegram bot token:", bold_body))
    story.append(Paragraph("1. Open Telegram, search @BotFather", body_style))
    story.append(Paragraph("2. Send /newbot, follow the prompts", body_style))
    story.append(Paragraph("3. Copy the token (looks like 123456:ABC-DEF1234...)", body_style))
    story.append(Spacer(1, 6))
    story.append(Paragraph("How to get your Telegram user ID:", bold_body))
    story.append(Paragraph("1. Search @userinfobot on Telegram", body_style))
    story.append(Paragraph("2. Send /start", body_style))
    story.append(Paragraph("3. Copy your ID number", body_style))

    # Step 5
    story.append(PageBreak())
    story.append(Paragraph("Step 5: Start the Bot", h1_style))
    story.append(Paragraph("1. In the smc-trading-bot folder", body_style))
    story.append(Paragraph("2. Double-click start_bot.bat", body_style))
    story.append(Paragraph("3. A console window opens showing logs", body_style))
    story.append(Paragraph("4. You should see: SMC Trading Bot is running!", body_style))
    story.append(Paragraph("5. Open Telegram on your phone and send /start to your bot", body_style))
    story.append(Spacer(1, 12))
    story.append(Paragraph("That's it. The bot is running.", bold_body))

    # Step 6
    story.append(Paragraph("Step 6: Auto-Start on VPS Reboot", h1_style))
    story.append(Paragraph("1. Press Win + R on the VPS", body_style))
    story.append(Paragraph("2. Type 'shell:startup' and press Enter", body_style))
    story.append(Paragraph("3. Right-click → New → Shortcut", body_style))
    story.append(Paragraph("4. Browse to start_bot.bat", body_style))
    story.append(Paragraph("5. Name it 'SMC Bot' → Finish", body_style))
    story.append(Paragraph("Now the bot restarts automatically whenever the VPS boots.", body_style))

    # Commands
    story.append(PageBreak())
    story.append(Paragraph("Step 7: Using the Bot from Telegram", h1_style))
    cmd_data = [
        ["What you want", "Command"],
        ["See the main menu", "/start"],
        ["Scan all symbols for signals", "/scan"],
        ["Analyze a specific pair", "/analyze EURUSD"],
        ["Run a backtest", "/backtest EURUSD H1 180"],
        ["Check open positions", "/positions"],
        ["See account balance", "/account"],
        ["View/change settings", "/settings"],
        ["Check session status", "/sessions"],
        ["Check news filter", "/news EURUSD"],
        ["Turn ON auto-trading", "/settings → Auto-Trade → Confirm"],
        ["Switch to live trading", "/settings → Mode → Live → Confirm"],
        ["Pause everything", "/pause"],
        ["Close all positions", "/close_all → Confirm"],
    ]
    story.append(make_table(cmd_data, col_widths=[220, 280]))

    # Quick Reference
    story.append(Paragraph("Quick Reference — Minimum Viable Setup", h1_style))
    story.append(Paragraph("1. RDP into VPS from phone", body_style))
    story.append(Paragraph("2. Install Python (check 'Add to PATH')", body_style))
    story.append(Paragraph("3. Install Git", body_style))
    story.append(Paragraph("4. Install MT5, log in to broker", body_style))
    story.append(Paragraph("5. Open CMD: git clone https://github.com/iamlexceez/smc-trading-bot.git", body_style))
    story.append(Paragraph("6. cd smc-trading-bot", body_style))
    story.append(Paragraph("7. Double-click setup.bat", body_style))
    story.append(Paragraph("8. Edit .env with your token, ID, and MT5 details", body_style))
    story.append(Paragraph("9. Double-click start_bot.bat", body_style))
    story.append(Paragraph("10. Message your bot on Telegram: /start", body_style))

    # Troubleshooting
    story.append(PageBreak())
    story.append(Paragraph("Troubleshooting", h1_style))
    trouble_data = [
        ["Problem", "Solution"],
        ["'Python is not recognized'", "Reinstall Python, check 'Add to PATH'"],
        ["'git is not recognized'", "Install Git from git-scm.com/download/win"],
        ["Bot starts but doesn't respond", "Check token in .env, check admin ID, make sure you're messaging the right bot"],
        ["'MT5 not available'", "Ensure MT5 is installed and running, check MT5 credentials in .env"],
        ["VPS disconnected, bot stopped", "Reconnect via RDP, double-click start_bot.bat again"],
        ["How to update the bot", "Open CMD in the folder, run: git pull, then start_bot.bat"],
    ]
    story.append(make_table(trouble_data, col_widths=[180, 320]))

    doc.build(story)
    print("✅ Generated Windows_VPS_Setup_Guide.pdf")


def generate_assessment_pdf():
    """Generate the bot assessment PDF."""
    doc = SimpleDocTemplate(
        os.path.join(OUTPUT_DIR, "Bot_Assessment_and_Roadmap.pdf"),
        pagesize=A4,
        rightMargin=50, leftMargin=50, topMargin=50, bottomMargin=50,
        title="SMC Trading Bot — Assessment & Roadmap",
        author="Perplexity Computer",
    )

    story = []
    story.append(Paragraph("SMC Trading Bot", title_style))
    story.append(Paragraph("Honest Assessment & Improvement Roadmap", subtitle_style))

    story.append(Paragraph("What Was Fixed (v2 Upgrade)", h1_style))
    fixed_data = [
        ["Weak Point", "Fix Applied"],
        ["No backtesting", "Full backtesting engine with historical replay, spread/slippage/commission, win rate, profit factor, Sharpe, Sortino, max drawdown, monthly returns"],
        ["Paper mode used fake data", "Real market data provider: MT5 → Twelve Data API → yfinance → synthetic fallback"],
        ["No entry confirmation", "Zone retest + candle patterns (engulfing, pin bar, inside bar, displacement)"],
        ["No trade management", "Breakeven at 1R, partial close at 2R, ATR trailing stop, time-based exit"],
        ["No session filtering", "London, New York, overlap sessions — skip low-liquidity hours"],
        ["No news filter", "ForexFactory economic calendar — blocks trading during high-impact events"],
        ["Basic CHoCH detection", "Tracks prior trend from 3rd swing point for proper reversal detection"],
        ["S/D freshness bugs", "Correctly checks if price has NOT revisited the zone since departure"],
        ["Settings buttons non-functional", "All buttons implemented: symbols, timeframes, spread, daily loss, trades, positions, cooldown"],
        ["No confirmation for dangerous actions", "Close all, live mode, auto-trade ON all require explicit confirmation"],
        ["DB_PATH ignored in Docker", "Uses os.getenv() for Docker volume persistence"],
    ]
    story.append(make_table(fixed_data, col_widths=[150, 350]))

    story.append(PageBreak())
    story.append(Paragraph("Current Strengths", h1_style))
    strengths = [
        ["Area", "Rating", "Notes"],
        ["Risk Management", "9/10", "10 hard gates, position sizing, daily limits — very solid"],
        ["Architecture", "8/10", "Clean separation, paper/live abstraction, configurable"],
        ["APA Concepts", "7/10", "Real SMC concepts implemented correctly at intermediate level"],
        ["Backtesting", "7/10", "Full metrics engine with realistic simulation"],
        ["Entry Logic", "7/10", "Zone retest + candle confirmation now implemented"],
        ["Trade Management", "7/10", "Breakeven, trailing, partial close, time exit"],
        ["Data Quality", "8/10", "Real market data from 3 sources + MT5"],
        ["Telegram UX", "8/10", "18 commands, full inline keyboard settings"],
        ["Safety", "9/10", "Paper by default, confirmations, news/session filters"],
    ]
    story.append(make_table(strengths, col_widths=[140, 60, 300]))

    story.append(Paragraph("Remaining Limitations", h1_style))
    story.append(Paragraph("These are areas that still need improvement but are not blockers:", body_style))
    limits = [
        ["Limitation", "Impact", "Priority"],
        ["Scoring weights not optimized", "Weights are reasonable but not backtested against real data", "High — run backtests and tune"],
        ["No multi-position correlation check", "Bot could open correlated trades (e.g., EURUSD + GBPUSD)", "Medium"],
        ["No DOM/order flow data", "Can't see real volume or order book depth", "Low — needs broker API"],
        ["Single entry strategy", "Only SMC zones + confirmation; no breakout or trend-following mode", "Medium"],
        ["No walk-forward optimization", "Can't auto-tune parameters on rolling windows", "Low — manual tuning for now"],
        ["Synthetic indices use fake data", "Deriv synthetic indices have no free API", "High — connect MT5 with Deriv"],
    ]
    story.append(make_table(limits, col_widths=[180, 220, 100]))

    story.append(PageBreak())
    story.append(Paragraph("Recommended Workflow", h1_style))
    story.append(Paragraph("1. Start in paper mode with auto-trade OFF", bold_body))
    story.append(Paragraph("Use /scan and /analyze to see signals. Compare to your own analysis.", body_style))
    story.append(Spacer(1, 6))
    story.append(Paragraph("2. Run backtests on your symbols", bold_body))
    story.append(Paragraph("Use /backtest EURUSD H1 180 to test 6 months of data. Check win rate, profit factor, and max drawdown. Run on multiple symbols and timeframes.", body_style))
    story.append(Spacer(1, 6))
    story.append(Paragraph("3. Tune the score threshold", bold_body))
    story.append(Paragraph("Start at 40%. If too many low-quality trades, raise to 60-70%. If too few trades, lower to 30%. The backtest will tell you the optimal threshold.", body_style))
    story.append(Spacer(1, 6))
    story.append(Paragraph("4. Connect MT5 with real data", bold_body))
    story.append(Paragraph("Keep paper mode but connect MT5 so analysis runs on real broker data. This makes paper testing meaningful.", body_style))
    story.append(Spacer(1, 6))
    story.append(Paragraph("5. Go live with tiny risk", bold_body))
    story.append(Paragraph("0.5% risk per trade maximum. Treat it as tuition. Track every trade manually.", body_style))
    story.append(Spacer(1, 6))
    story.append(Paragraph("6. After 50-100 trades, adjust weights", bold_body))
    story.append(Paragraph("See which factors correlate with winners. Adjust the scoring weights accordingly.", body_style))

    story.append(Paragraph("Backtest Commands Reference", h1_style))
    story.append(Paragraph("From Telegram:", bold_body))
    story.append(Paragraph("/backtest EURUSD H1 180 — 6 months of H1 data", code_style))
    story.append(Paragraph("/backtest XAUUSD H4 365 — 1 year of H4 data", code_style))
    story.append(Paragraph("/backtest GBPUSD M15 90 — 3 months of M15 data", code_style))
    story.append(Spacer(1, 6))
    story.append(Paragraph("From CLI:", bold_body))
    story.append(Paragraph("python -m backtest.runner --symbol EURUSD --timeframe H1 --days 180 --score 50 --rr 3.0 --risk 1.0", code_style))

    story.append(Paragraph("Key Metrics to Watch", h1_style))
    metrics_data = [
        ["Metric", "Good", "Bad", "What it means"],
        ["Win Rate", ">45%", "<35%", "Percentage of winning trades"],
        ["Profit Factor", ">1.5", "<1.0", "Gross profit divided by gross loss"],
        ["Expectancy", ">0", "<0", "Average P&L per trade"],
        ["Max Drawdown", "<10%", ">20%", "Largest peak-to-trough decline"],
        ["Sharpe Ratio", ">1.0", "<0.5", "Risk-adjusted return"],
        ["Avg RR", ">1.5", "<1.0", "Average risk-reward achieved"],
    ]
    story.append(make_table(metrics_data, col_widths=[90, 55, 55, 250]))

    doc.build(story)
    print("✅ Generated Bot_Assessment_and_Roadmap.pdf")


if __name__ == "__main__":
    generate_spec_pdf()
    generate_deploy_pdf()
    generate_assessment_pdf()
    print("\nAll PDFs generated in", OUTPUT_DIR)
