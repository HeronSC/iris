@echo off
cd /d "%~dp0..\.."
start "" /b cmd /c "timeout /t 1 >nul & start http://127.0.0.1:8790/?dev"
python -m ui.room.serve 8790
