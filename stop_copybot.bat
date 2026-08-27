@echo off
rem Graceful stop. The POSITION IS KEPT — stopping the bot must not flatten a
rem healthy book; it only stops managing it.
cd /d %~dp0

if not exist logs\copybot.pid ( echo Not running. & pause & exit /b 0 )
set /p PID=<logs\copybot.pid

taskkill /pid %PID% >nul 2>&1
timeout /t 10 >nul
tasklist /fi "PID eq %PID%" 2>nul | find "%PID%" >nul
if not errorlevel 1 (
  echo Process still alive, forcing...
  taskkill /f /pid %PID% >nul 2>&1
)
del logs\copybot.pid 2>nul
echo Copybot stopped. Any open position is UNMANAGED until you start it again.
pause
