@echo off
REM ============================================================
REM   SMC Trading Bot — Windows VPS One-Click Setup
REM   Run this on your Windows VPS in PowerShell or CMD.
REM   It will install everything and set up the bot.
REM ============================================================

echo.
echo ============================================
echo   SMC Trading Bot — Windows VPS Setup
echo ============================================
echo.

REM Check if Python is installed
python --version >nul 2>&1
if %errorlevel% neq 0 (
    echo [SETUP] Python not found. Downloading installer...
    echo.
    echo Please install Python 3.11+ from:
    echo   https://www.python.org/downloads/
    echo.
    echo IMPORTANT: Check "Add Python to PATH" during installation!
    echo Then re-run this script.
    echo.
    start https://www.python.org/downloads/
    pause
    exit /b 1
)

echo [OK] Python found:
python --version
echo.

REM Check if Git is installed
git --version >nul 2>&1
if %errorlevel% neq 0 (
    echo [SETUP] Git not found. Downloading installer...
    start https://git-scm.com/download/win
    echo Please install Git, then re-run this script.
    pause
    exit /b 1
)

echo [OK] Git found.
echo.

REM Clone or update the repo
if exist smc-trading-bot (
    echo [UPDATE] Repo exists. Pulling latest...
    cd smc-trading-bot
    git pull
) else (
    echo [CLONE] Cloning repository...
    git clone https://github.com/iamlexceez/smc-trading-bot.git
    cd smc-trading-bot
)
echo.

REM Create virtual environment
if not exist venv (
    echo [SETUP] Creating virtual environment...
    python -m venv venv
)
echo [OK] Virtual environment ready.
echo.

REM Activate and install
echo [SETUP] Installing dependencies...
call venv\Scripts\activate.bat
python -m pip install --upgrade pip
pip install -r requirements.txt
pip install -r requirements-mt5.txt
echo.
echo [OK] Dependencies installed.
echo.

REM Create .env from template if it doesn't exist
if not exist .env (
    echo [SETUP] Creating .env file from template...
    copy .env.example .env >nul
    echo.
    echo ============================================
    echo   ACTION REQUIRED: Edit .env file
    echo ============================================
    echo.
    echo Open the .env file in Notepad and fill in:
    echo   TELEGRAM_BOT_TOKEN  - from @BotFather on Telegram
    echo   TELEGRAM_ADMIN_IDS  - from @userinfobot on Telegram
    echo   MT5_LOGIN          - your MT5 account number
    echo   MT5_PASSWORD       - your MT5 password
    echo   MT5_SERVER         - your broker server
    echo.
    echo Then run: start_bot.bat
    echo.
    notepad .env
) else (
    echo [OK] .env file already exists.
)

echo.
echo ============================================
echo   Setup Complete!
echo ============================================
echo.
echo To start the bot:  start_bot.bat
echo To stop the bot:   Press Ctrl+C in the bot window
echo.

REM Create start_bot.bat
echo @echo off > start_bot.bat
echo cd /d "%~dp0" >> start_bot.bat
echo call venv\Scripts\activate.bat >> start_bot.bat
echo python main.py >> start_bot.bat
echo pause >> start_bot.bat

echo Created start_bot.bat — double-click to run the bot.
echo.
pause
