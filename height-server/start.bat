@echo off
REM Building Height Server - start on the DSM machine.
REM
REM   start.bat                                  use the built-in folder paths
REM   start.bat "D:\dsm" "D:\dtm"                point it somewhere else
REM
REM The paths are quoted everywhere below because the default ones contain a
REM space, and an unquoted "DSM Data" becomes two arguments.

setlocal
cd /d "%~dp0"

if not "%~1"=="" set "HS_DSM_DIR=%~1"
if not "%~2"=="" set "HS_DTM_DIR=%~2"
if "%HS_PORT%"=="" set "HS_PORT=8900"

if not exist ".venv\Scripts\python.exe" (
  echo [*] First run - creating .venv and installing dependencies.
  echo     This takes a few minutes once, then never again.
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

echo.
echo [*] Checking the elevation data before serving...
".venv\Scripts\python.exe" selfcheck.py
if errorlevel 1 (
  echo.
  echo [!] The self-check failed. Fix the problem above, then run start.bat again.
  echo     Most often this is a wrong folder path - pass the right ones:
  echo         start.bat "C:\path\to\DSM Data" "C:\path\to\DTM Data"
  exit /b 1
)

echo.
echo [*] Starting Building Height Server on port %HS_PORT%
echo     Leave this window open. Ctrl+C stops it.
echo.
".venv\Scripts\python.exe" -m uvicorn app.main:app --host 0.0.0.0 --port %HS_PORT%
endlocal
