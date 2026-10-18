@echo off
rem ============================================================
rem  Urbanly - start everything the webapp needs on this laptop
rem    1. Ollama  (Qwen2.5-VL 3B, answers the "Ask the survey" box)
rem    2. Local data server (imagery, layers, /ask) + public tunnel
rem  Ctrl+C in this window stops the data server and marks it offline.
rem ============================================================
title Urbanly - local stack
cd /d "%~dp0"
if "%GEO_DATA_DIR%"=="" set GEO_DATA_DIR=D:\GEO_SERVE
set ASK_MODEL=qwen2.5vl:3b

echo.
echo [1/3] Ollama
rem Ollama normally auto-starts with Windows; only start it if the API is down.
curl -s -o nul -m 4 http://127.0.0.1:11434/api/tags
if errorlevel 1 (
    echo       not running - starting it
    start "" /min ollama serve
    rem give the API a moment to come up
    for /l %%i in (1,1,20) do (
        curl -s -o nul -m 2 http://127.0.0.1:11434/api/tags && goto :ollama_up
        ping -n 2 127.0.0.1 >nul
    )
    :ollama_up
) else (
    echo       already running
)

echo.
echo [2/3] Loading %ASK_MODEL% into memory
rem The first question is slow while the model loads; do it now instead.
curl -s -m 300 http://127.0.0.1:11434/api/chat -H "content-type: application/json" ^
  -d "{\"model\":\"%ASK_MODEL%\",\"stream\":false,\"messages\":[{\"role\":\"user\",\"content\":\"ok\"}]}" >nul
if errorlevel 1 (
    echo       WARNING: model did not respond - run:  ollama pull %ASK_MODEL%
) else (
    echo       ready
)

echo.
echo [3/3] Data server on %GEO_DATA_DIR%
echo.
python -u run.py --port 8777 %*

echo.
pause
