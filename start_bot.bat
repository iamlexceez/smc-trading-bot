@echo off
setlocal EnableExtensions

REM Start exactly one foreground bot process from the repository root and retain
REM all startup/runtime errors for VPS diagnosis.
cd /d "%~dp0"
if not exist "logs" mkdir "logs"

echo [%date% %time%] Starting SMC Trading Bot from "%CD%" >> "logs\bot_runtime.log"
if not exist "venv\Scripts\python.exe" (
    echo [%date% %time%] ERROR: venv\Scripts\python.exe was not found. >> "logs\bot_runtime.log"
    echo ERROR: The bot virtual environment was not found. See logs\bot_runtime.log
    pause
    exit /b 1
)

powershell -NoProfile -ExecutionPolicy Bypass -Command "& { & .\venv\Scripts\python.exe -u .\main.py 2>&1 | Tee-Object -FilePath .\logs\bot_runtime.log -Append; exit $LASTEXITCODE }"
set EXIT_CODE=%ERRORLEVEL%
echo [%date% %time%] Bot process exited with code %EXIT_CODE%. >> "logs\bot_runtime.log"
echo Bot process exited with code %EXIT_CODE%. Review logs\bot_runtime.log
pause
exit /b %EXIT_CODE%
