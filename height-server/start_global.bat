@echo off
REM Building Height Server - reachable from anywhere, not just this network.
REM
REM   start_global.bat                           public HTTPS URL via Cloudflare
REM   start_global.bat "D:\dsm" "D:\dtm"         with different data folders
REM
REM cloudflared makes only an OUTBOUND connection, so no ports are opened on
REM this laptop, no router setting changes, and no admin rights are needed.
REM
REM An API key is REQUIRED in this mode and is generated for you on the first
REM run, then reused. See the URL and key printed at the end.

setlocal
cd /d "%~dp0"

if not exist ".venv\Scripts\python.exe" (
  echo [*] First run - creating .venv and installing dependencies.
  py -3 -m venv .venv || python -m venv .venv
  if errorlevel 1 (
    echo [!] Could not create a virtual environment. Is Python 3.10+ installed
    echo     and on PATH?  Check with:  python --version
    exit /b 1
  )
  ".venv\Scripts\python.exe" -m pip install --upgrade pip
  ".venv\Scripts\python.exe" -m pip install -r requirements.txt
  if errorlevel 1 (
    echo [!] Dependency install failed - see the error above.
    exit /b 1
  )
)

if "%~1"=="" (
  ".venv\Scripts\python.exe" serve_global.py
) else (
  ".venv\Scripts\python.exe" serve_global.py --dsm "%~1" --dtm "%~2"
)
endlocal
