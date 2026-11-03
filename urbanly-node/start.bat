@echo off
REM ============================================================
REM  Urbanly — double-click this. Nothing to set up, nothing to type.
REM
REM  On the first run it works out this machine's settings by itself:
REM  where the survey data is, what this computer is called, and which
REM  other laptop on your Tailscale network is running Urbanly. Those
REM  go into config.env, which you can edit later if you want to.
REM
REM  Then it syncs with the other laptop, starts the model server and
REM  the data server, and shows what is running.
REM
REM  Both laptops run this same file. Neither is the master.
REM
REM  Optional, for anyone who prefers typing:
REM     start.bat status     what I have and who is reachable
REM     start.bat sync       pull newer files, start nothing
REM ============================================================
setlocal
cd /d "%~dp0"
title Urbanly

echo.
echo   Urbanly - starting up
echo   ---------------------

REM Prefer a project virtual environment when one exists, so this shares the
REM already-installed torch rather than asking for its own copy.
set "PY="
if exist "..\gpu-server\.venv\Scripts\python.exe" set "PY=..\gpu-server\.venv\Scripts\python.exe"
if exist ".venv\Scripts\python.exe" set "PY=.venv\Scripts\python.exe"

REM On a laptop set up by SETUP SERVER.bat the project is installed somewhere
REM else entirely and this folder is only a copy, so the environment holding
REM PyTorch is nowhere near "..\gpu-server". config.env records where it went.
if exist "config.env" (
  for /f "usebackq tokens=1,* delims==" %%a in (`findstr /b "UN_GPU_SERVER_DIR=" config.env`) do set "UN_GSD=%%b"
)
if defined UN_GSD if exist "%UN_GSD%\.venv\Scripts\python.exe" set "PY=%UN_GSD%\.venv\Scripts\python.exe"
if not defined PY (
  where python >nul 2>nul
  if errorlevel 1 (
    echo.
    echo   [!] Python was not found.
    echo       Install Python 3.10 or newer, tick "Add python.exe to PATH",
    echo       then double-click this file again.
    echo.
    pause
    exit /b 1
  )
  set "PY=python"
)

"%PY%" -c "import fastapi, uvicorn, requests" >nul 2>nul
if errorlevel 1 (
  echo   installing the few packages this needs, one time only...
  "%PY%" -m pip install --quiet --disable-pip-version-check fastapi "uvicorn[standard]" requests
  if errorlevel 1 (
    echo.
    echo   [!] That install failed. Check the internet connection and retry.
    echo.
    pause
    exit /b 1
  )
)

if "%~1"=="" goto run
if /i "%~1"=="status" ( "%PY%" node.py status & goto hold )
if /i "%~1"=="sync"   ( "%PY%" node.py sync %2 & goto hold )
"%PY%" node.py up %1 %2 %3
goto hold

:run
"%PY%" node.py up

:hold
echo.
echo   This window can be closed. The servers keep running in their own windows.
echo.
if not defined UN_NO_PAUSE pause
endlocal
