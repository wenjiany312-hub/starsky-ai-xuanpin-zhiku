@echo off
cd /d "%~dp0"
set "PY=%~dp0backend\.venv\Scripts\python.exe"
if not exist "%PY%" set "PY=python"
"%PY%" desktop_launcher.py
echo.
pause
