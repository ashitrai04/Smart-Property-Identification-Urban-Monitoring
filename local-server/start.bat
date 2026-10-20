@echo off
rem Turn on the local data server (serves D:\GEO_SERVE to the webapp).
rem Double-click this file, or run it from a terminal. Ctrl+C stops it.
title Local geo server
cd /d "%~dp0"
if "%GEO_DATA_DIR%"=="" set GEO_DATA_DIR=D:\GEO_SERVE
python run.py --data "%GEO_DATA_DIR%" %*
echo.
pause
