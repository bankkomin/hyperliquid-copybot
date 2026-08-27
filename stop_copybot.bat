@echo off
rem Graceful stop. The POSITION IS KEPT — stopping the bot must not liquidate a
rem healthy book; it only stops managing it and pulls our resting orders.
rem
rem The request goes through the database, not a signal: pythonw.exe has no
rem window to receive WM_CLOSE, so taskkill can only force-kill and any
rem finally-block cleanup would never run.
cd /d %~dp0

if not exist logs\copybot.pid ( echo Not running. & pause & exit /b 0 )
set /p PID=<logs\copybot.pid

echo Requesting graceful shutdown...
venv\Scripts\python.exe -m src.stop
if errorlevel 1 (
  echo Could not write the stop request; forcing.
  goto :force
)

rem The bot polls every 2s and cancels its resting orders before exiting.
for /l %%i in (1,1,20) do (
  tasklist /fi "PID eq %PID%" 2>nul | find "%PID%" >nul
  if errorlevel 1 goto :done
  timeout /t 2 >nul
)

:force
echo Still alive after 40s, forcing...
taskkill /f /pid %PID% >nul 2>&1

:done
del logs\copybot.pid 2>nul
echo Copybot stopped. Any open position is UNMANAGED until you start it again.
pause
