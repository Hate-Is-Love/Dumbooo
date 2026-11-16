@echo off
rem Forgot your DUMBOO password? This sets a new one (on this PC only). Nothing else is deleted.
cd /d "%~dp0"
set "PY=python"
if exist ".venv\Scripts\python.exe" set "PY=.venv\Scripts\python.exe"
"%PY%" scripts/reset_password.py
pause
