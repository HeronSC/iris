@echo off
setlocal
cd /d %~dp0
powershell -ExecutionPolicy Bypass -File "%~dp0dev_run.ps1" -Mode ui
endlocal
