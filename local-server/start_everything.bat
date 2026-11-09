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
rem
rem  Building heights come from a SECOND laptop, the one holding the DSM and
rem  DTM folders (see height-server/README.md). It is reached over Tailscale,
rem  which is set below and needs no further thought. To point somewhere else:
rem
rem    same wifi     set HEIGHT_SERVER=http://192.168.88.3:8900
rem    over a tunnel set HEIGHT_SERVER=auto      (looks the address up)
rem
rem  Unset it and everything else still works - the webapp simply says no
rem  height server is configured when heights are asked for.
rem ============================================================
title Urbanly - full stack
cd /d "%~dp0"

if "%GEO_DATA_DIR%"=="" set GEO_DATA_DIR=D:\GEO_SERVE
if "%ASK_MODEL%"=="" set ASK_MODEL=qwen2.5vl:3b
if "%GPU_SERVER_DIR%"=="" set GPU_SERVER_DIR=%~dp0..\gpu-server

rem The DSM laptop's address lives in height.local.cmd, which is NOT in git: this is
rem a public repo, and the address is per-machine anyway. Copy
rem height.local.cmd.example to height.local.cmd and put yours in it.
rem `start` passes the environment on, so it reaches the model window.
if exist "%~dp0height.local.cmd" call "%~dp0height.local.cmd"

rem ---------- pick the interpreter, once, explicitly ----------
rem Every server below used to call a bare `python`, which means they ran
rem under whatever happened to be first on PATH in the window you launched
rem from. Start this from a shell with a venv active and the servers inherit
rem that venv; start it from Explorer and they get a different one. That is
rem how building delineation came to fail with "No module named 'yacs'" on a
rem machine where yacs was installed - just not in the interpreter that
rem actually got used. Resolve it once here and pass it down.
set "PY="
if not "%URBANLY_PYTHON%"=="" set "PY=%URBANLY_PYTHON%"
if "%PY%"=="" if exist "%~dp0..\.venv\Scripts\python.exe" set "PY=%~dp0..\.venv\Scripts\python.exe"
if "%PY%"=="" if exist "%~dp0..\..\.venv\Scripts\python.exe" set "PY=%~dp0..\..\.venv\Scripts\python.exe"
if "%PY%"=="" if exist "%GPU_SERVER_DIR%\.venv\Scripts\python.exe" set "PY=%GPU_SERVER_DIR%\.venv\Scripts\python.exe"
if "%PY%"=="" for %%p in (python.exe) do set "PY=%%~$PATH:p"
if "%PY%"=="" (
    echo   FATAL: no python found. Set URBANLY_PYTHON to the interpreter to use.
    pause
    exit /b 1
)
echo   python : %PY%

rem ---------- the model server's own settings ----------
rem gpu-server/app/config.py says its settings come from config.env, which
rem only run.sh reads - and run.sh is the Linux path. Without this the model
rem server starts with no R2 credentials and answers 500 to every
rem /r2/presign and /predict-url, which from the webapp looks like "the
rem models are broken" rather than "nobody told it the keys".
for %%F in ("%~dp0..\.env" "%GPU_SERVER_DIR%\config.env") do (
    if exist %%F for /f "usebackq eol=# tokens=1,* delims==" %%a in (%%F) do (
        if not "%%~b"=="" set "%%a=%%~b"
    )
)
rem Headless matplotlib, and return freed CUDA blocks instead of hoarding
rem them - the difference between a job finishing and an out-of-memory error
rem half way through on a 6 GB card.
if "%MPLBACKEND%"=="" set MPLBACKEND=Agg
if "%PYTORCH_CUDA_ALLOC_CONF%"=="" set PYTORCH_CUDA_ALLOC_CONF=expandable_segments:True
if "%HF_HOME%"=="" set "HF_HOME=%GPU_SERVER_DIR%\weights\hf"
if "%YOLO_CONFIG_DIR%"=="" set "YOLO_CONFIG_DIR=%GPU_SERVER_DIR%\data\.ultralytics"

rem ---------- fail here, not in a QGIS dialog ----------
rem The model server imports these lazily, so a missing one surfaces much
rem later as a 503 in whatever happened to ask first. Checking now costs a
rem second and names the problem while someone is still looking at a console.
"%PY%" -c "import torch, timm, yacs, affine, cv2, rasterio, fastapi" 2>nul
if errorlevel 1 (
    echo.
    echo   WARNING: that interpreter is missing packages the models need.
    "%PY%" -c "import importlib;[print('            missing:',m) for m in ('torch','timm','yacs','affine','cv2','rasterio','fastapi','ultralytics') if not __import__('importlib.util',fromlist=['x']).find_spec(m)]"
    echo   Fix with:  "%PY%" -m pip install ^<name^>
    echo   or run SETUP SERVER.bat once on this machine.
    echo.
)

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
if "%HEIGHT_SERVER%"=="" (
    echo   heights: not configured ^(set HEIGHT_SERVER - see the notes above^)
) else (
    echo   heights: %HEIGHT_SERVER%
)
echo ============================================================

rem ---------- 1. Ollama ----------
echo.
if "%SKIP_ASK%"=="1" (
    echo [1/5] Ollama              skipped ^(--no-ask^)
    goto after_ask
)
echo [1/5] Ollama
curl -s -o nul -m 4 http://127.0.0.1:11434/api/tags
if not errorlevel 1 (
    echo       already running
    goto ollama_up
)
echo       not running - starting it
start "" /min ollama serve
rem The wait loop and its exit label stay at the top level: cmd parses a
rem parenthesised block in one go, so a label written inside one is a syntax
rem error - "^) was unexpected at this time" - before a single line runs.
for /l %%i in (1,1,20) do (
    curl -s -o nul -m 2 http://127.0.0.1:11434/api/tags && goto ollama_up
    ping -n 2 127.0.0.1 >nul
)
echo       WARNING: no answer in 40s - the chat box will be offline
:ollama_up

rem ---------- 2. warm the chat model ----------
echo.
echo [2/5] Loading %ASK_MODEL%
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
    echo [3/5] Model server        skipped ^(--no-gpu^)
    goto after_gpu
)
echo [3/5] Model server
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
start "Urbanly - models" cmd /k "cd /d "%GPU_SERVER_DIR%" && "%PY%" -m uvicorn main:app --app-dir app --host 0.0.0.0 --port 8800"
for /l %%i in (1,1,30) do (
    curl -s -o nul -m 2 http://127.0.0.1:8800/api/health && goto gpu_up
    ping -n 2 127.0.0.1 >nul
)
echo       WARNING: did not answer in 60s - check the "Urbanly - models" window
goto after_gpu
:gpu_up
echo       ready on :8800
for /f "delims=" %%r in ('curl -s -m 5 http://127.0.0.1:8800/hisup/health') do echo       hisup: %%r
if not "%HEIGHT_SERVER%"=="" (
    for /f "delims=" %%r in ('curl -s -m 8 http://127.0.0.1:8800/height/health') do echo       height: %%r
)
:after_gpu

rem ---------- 4. node service ----------
rem Peer sync with the other laptop. Not needed by the webapp or QGIS, but
rem starting it here means this command and START URBANLY.bat leave the
rem machine in the same state, so it does not matter which one you run.
echo.
echo [4/5] Node service
curl -s -o nul -m 4 http://127.0.0.1:8790/node/status
if not errorlevel 1 (
    echo       already running on :8790
) else if exist "%~dp0..\urbanly-node\node_api.py" (
    start "Urbanly - node" cmd /k "cd /d "%~dp0..\urbanly-node" && "%PY%" -m uvicorn node_api:app --host 0.0.0.0 --port 8790"
    echo       starting on :8790
) else (
    echo       urbanly-node not present - skipped
)

rem ---------- 5. data server (foreground) ----------
echo.
echo [5/5] Data server on %GEO_DATA_DIR%
echo       Ctrl+C stops it and marks the site offline.
echo.
"%PY%" -u run.py --port 8777 %PASSTHRU%

echo.
echo Data server stopped. The model window stays open - close it to stop models too.
pause
