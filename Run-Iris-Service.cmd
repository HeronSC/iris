@echo off
cd /d E:\AI\Iris
"E:\AI\Iris\Runtime\venv\Scripts\python.exe" iris_service.py --console
if errorlevel 1 (
  echo.
  echo The Iris host exited with an error. Press any key to close this window.
  pause >nul
)
