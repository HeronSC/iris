@echo off
cd /d E:\AI\Iris
"E:\AI\Iris\Runtime\venv\Scripts\python.exe" main.py
if errorlevel 1 (
  echo.
  echo Iris exited with an error. Press any key to close this window.
  pause >nul
)
