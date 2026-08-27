@echo off
rem Double-click to run. First run installs everything; later runs just start.
cd /d %~dp0

rem ---------- auto-setup ----------
if not exist venv\Scripts\python.exe (
  echo [setup] Creating virtual environment...
  py -3 -m venv venv 2>nul || python -m venv venv
  if not exist venv\Scripts\python.exe (
    echo [setup] FAILED: install Python 3.10+ from python.org, then run this again.
    pause & exit /b 1
  )
)
fc /b requirements.txt venv\.installed >nul 2>&1
if errorlevel 1 (
  echo [setup] Installing dependencies...
  venv\Scripts\python.exe -m pip install --upgrade pip --quiet
  venv\Scripts\python.exe -m pip install -r requirements.txt --quiet
  if errorlevel 1 ( echo [setup] pip install FAILED & pause & exit /b 1 )
  copy /y requirements.txt venv\.installed >nul
)
if not exist config.yaml copy config.example.yaml config.yaml >nul
if not exist logs mkdir logs
if not exist data mkdir data
if not exist reports mkdir reports

rem ---------- stale-pid guard ----------
rem A crash or reboot leaves the pid file behind. Only refuse to start when that
rem PID is genuinely still alive AND is one of ours, or the VPS would never
rem auto-restart itself. No pause on this path: on a headless VPS an unattended
rem restart would block forever on it.
if exist logs\copybot.pid call :check_stale
if "%STILL_RUNNING%"=="1" (
  echo Copybot is already running. Use stop_copybot.bat first.
  exit /b 1
)

rem ---------- launch ----------
echo Starting copybot in the background...
start "" venv\Scripts\pythonw.exe -m src.main
timeout /t 5 >nul
if not exist logs\copybot.pid (
  echo WARNING: no pid file yet. Check logs\copybot.jsonl for errors.
)
start http://localhost:8061
exit /b 0

:check_stale
set STILL_RUNNING=
set OLDPID=
set /p OLDPID=<logs\copybot.pid
if "%OLDPID%"=="" (
  echo [setup] Empty pid file, removing.
  del logs\copybot.pid
  goto :eof
)
rem Filter on IMAGENAME too: Windows recycles PIDs, and matching a random
rem svchost would block the restart of a bot that is actually dead.
tasklist /fi "PID eq %OLDPID%" /fi "IMAGENAME eq pythonw.exe" 2>nul | find "%OLDPID%" >nul
if not errorlevel 1 (
  set STILL_RUNNING=1
  goto :eof
)
echo [setup] Removing stale pid file from PID %OLDPID% ^(no pythonw with that id^).
del logs\copybot.pid
goto :eof
