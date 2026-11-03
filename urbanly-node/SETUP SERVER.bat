@echo off
REM ============================================================
REM  Run this ONCE on a laptop that only has the node package,
REM  to turn it into a full Urbanly server.
REM
REM  It fetches the project, builds a virtual environment beside
REM  it, installs PyTorch and the rest, sizes the settings to this
REM  machine's graphics card, and pulls the model weights from the
REM  other laptop.
REM
REM  Takes a while - PyTorch alone is about 2.5 GB.
REM
REM    SETUP SERVER.bat                install into C:\Urbanly
REM    SETUP SERVER.bat E:\Urbanly     somewhere else
REM ============================================================
setlocal
cd /d "%~dp0"
title Urbanly - server setup

where python >nul 2>nul
if errorlevel 1 (
  echo.
  echo   [!] Python was not found.
  echo       Install Python 3.10 or newer, tick "Add python.exe to PATH",
  echo       then run this again.
  echo.
  pause
  exit /b 1
)

if "%~1"=="" (
  python setup_server.py
) else (
  python setup_server.py --dir "%~1"
)

echo.
if not defined UN_NO_PAUSE pause
endlocal
