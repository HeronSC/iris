@echo off
cd /d "%~dp0"
".venv\Scripts\python.exe" ui\main.py
if errorlevel 1 (
  echo.
  echo Iris exited with an error. Press any key to close this window.
  pause >nul
)
