@echo off
REM ============================================================
REM  Urbanly node — one command, either laptop.
REM
REM    start.bat              sync with peers, then start everything
REM    start.bat status       what I have and who is reachable
REM    start.bat sync         pull newer files, start nothing
REM    start.bat --no-sync    start without syncing first
REM    start.bat --no-tunnel  start without a public address
REM
REM  Both laptops run this same file. Neither is the master: whichever
REM  is running serves, and on sync the newer file wins.
REM
REM  Settings live in config.env — copy config.env.example to start.
REM ============================================================
setlocal
cd /d "%~dp0"
title Urbanly node

if not exist "config.env" (
  echo.
  echo   config.env is missing.
  echo   Copy config.env.example to config.env and set UN_DATA_DIR,
  echo   UN_PEERS and UN_API_KEY for this machine, then run again.
  echo.
  exit /b 1
)

REM A venv is used when one exists, so this shares the project's installed
REM packages rather than demanding its own copy of torch.
set "PY=python"
if exist "..\gpu-server\.venv\Scripts\python.exe" set "PY=..\gpu-server\.venv\Scripts\python.exe"
if exist ".venv\Scripts\python.exe" set "PY=.venv\Scripts\python.exe"

"%PY%" -c "import fastapi, uvicorn, requests" 2>nul
if errorlevel 1 (
  echo.
  echo   Installing the few packages this needs...
  "%PY%" -m pip install --quiet fastapi "uvicorn[standard]" requests
  if errorlevel 1 (
    echo   [!] Install failed. Is Python 3.10+ on PATH?  python --version
    exit /b 1
  )
)

if "%~1"=="" (
  "%PY%" node.py up
) else if /i "%~1"=="status" (
  "%PY%" node.py status
) else if /i "%~1"=="sync" (
  "%PY%" node.py sync %2 %3
) else (
  "%PY%" node.py up %1 %2 %3
)

REM Hold the window open when this was double-clicked, so the report can be
REM read. Skipped when driven from a terminal or a script, where a pause is
REM just something else that has to be killed.
echo.
if not defined UN_NO_PAUSE pause
endlocal
