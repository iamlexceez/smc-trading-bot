@echo off
REM Auto-start the SMC Trading Bot when VPS boots
cd /d "%~dp0"
call venv\Scripts\activate.bat
python main.py
