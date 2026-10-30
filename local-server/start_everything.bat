@echo off
rem ============================================================
rem  Urbanly - one command, everything the webapp needs.
rem
rem    1. Ollama              answers the "Ask the survey" chat box
rem    2. Qwen2.5-VL warm     so the first question is not the slow one
rem    3. GPU model server    SegFormer, pothole, HiSup buildings
rem    4. Data server         imagery, layers, /ask, + public tunnel
rem
rem  The data server is last and runs in the foreground: Ctrl+C here stops
rem  it and marks the site offline, which is the behaviour you want from a
rem  window you are watching. The GPU server runs behind it in its own
rem  window, so a model crash does not take the imagery down with it.
rem
rem  Options:
rem    start_everything.bat --no-gpu     skip the model server (data only)
rem    start_everything.bat --no-ask     skip Ollama (no chat)
rem ============================================================
title Urbanly - full stack
cd /d "%~dp0"

if "%GEO_DATA_DIR%"=="" set GEO_DATA_DIR=D:\GEO_SERVE
if "%ASK_MODEL%"=="" set ASK_MODEL=qwen2.5vl:3b
if "%GPU_SERVER_DIR%"=="" set GPU_SERVER_DIR=%~dp0..\gpu-server

set SKIP_GPU=0
set SKIP_ASK=0
set PASSTHRU=
:args
if "%~1"=="" goto done_args
if /i "%~1"=="--no-gpu" (set SKIP_GPU=1) else if /i "%~1"=="--no-ask" (set SKIP_ASK=1) else (set PASSTHRU=%PASSTHRU% %1)
shift
goto args
:done_args

echo.
echo ============================================================
echo   Urbanly local stack
echo   data   : %GEO_DATA_DIR%
echo   models : %GPU_SERVER_DIR%
echo ============================================================

rem ---------- 1. Ollama ----------
echo.
if "%SKIP_ASK%"=="1" (
    echo [1/4] Ollama              skipped ^(--no-ask^)
    goto after_ask
)
echo [1/4] Ollama
curl -s -o nul -m 4 http://127.0.0.1:11434/api/tags
if errorlevel 1 (
    echo       not running - starting it
    start "" /min ollama serve
    for /l %%i in (1,1,20) do (
        curl -s -o nul -m 2 http://127.0.0.1:11434/api/tags && goto ollama_up
        ping -n 2 127.0.0.1 >nul
    )
    :ollama_up
) else (
    echo       already running
)

rem ---------- 2. warm the chat model ----------
echo.
echo [2/4] Loading %ASK_MODEL%
curl -s -m 300 http://127.0.0.1:11434/api/chat -H "content-type: application/json" ^
  -d "{\"model\":\"%ASK_MODEL%\",\"stream\":false,\"messages\":[{\"role\":\"user\",\"content\":\"ok\"}]}" >nul
if errorlevel 1 (
    echo       WARNING: no response - run:  ollama pull %ASK_MODEL%
) else (
    echo       ready
)
:after_ask

rem ---------- 3. GPU model server ----------
echo.
if "%SKIP_GPU%"=="1" (
    echo [3/4] Model server        skipped ^(--no-gpu^)
    goto after_gpu
)
echo [3/4] Model server
curl -s -o nul -m 4 http://127.0.0.1:8800/api/health
if not errorlevel 1 (
    echo       already running on :8800
    goto after_gpu
)
if not exist "%GPU_SERVER_DIR%\app\main.py" (
    echo       not found at %GPU_SERVER_DIR% - skipping
    echo       ^(set GPU_SERVER_DIR if it lives elsewhere^)
    goto after_gpu
)
echo       starting in its own window
rem Its own window on purpose: a model that runs out of GPU memory should not
rem take the imagery server down with it.
start "Urbanly - models" cmd /k "cd /d "%GPU_SERVER_DIR%" && python -m uvicorn main:app --app-dir app --host 0.0.0.0 --port 8800"
for /l %%i in (1,1,30) do (
    curl -s -o nul -m 2 http://127.0.0.1:8800/api/health && goto gpu_up
    ping -n 2 127.0.0.1 >nul
)
echo       WARNING: did not answer in 60s - check the "Urbanly - models" window
goto after_gpu
:gpu_up
echo       ready on :8800
for /f "delims=" %%r in ('curl -s -m 5 http://127.0.0.1:8800/hisup/health') do echo       hisup: %%r
:after_gpu

rem ---------- 4. data server (foreground) ----------
echo.
echo [4/4] Data server on %GEO_DATA_DIR%
echo       Ctrl+C stops it and marks the site offline.
echo.
python -u run.py --port 8777 %PASSTHRU%

echo.
echo Data server stopped. The model window stays open - close it to stop models too.
pause
