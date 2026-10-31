@echo off
rem ============================================================
rem  Public HTTPS address for this GPU server (Windows).
rem
rem  The Vercel site is HTTPS, so it cannot call http://127.0.0.1 - the
rem  browser blocks mixed content. cloudflared makes an OUTBOUND connection
rem  only: no open ports, no router changes.
rem
rem    tunnel.bat            start the tunnel and publish its address
rem    tunnel.bat --stop     stop it and mark the server offline
rem
rem  The free quick tunnel gets a NEW address every restart, so the address is
rem  published to R2 (runtime/gpu_server.json); the data backend serves it at
rem  /api/gpu-server and the webapp reads it, which is why a restart needs no
rem  redeploy. That publish needs R2 keys in config.env - without them the
rem  tunnel still runs and prints its address, but you must point the webapp
rem  at it yourself (VITE_GPU_API).
rem
rem  This is the Windows counterpart of tunnel.sh, which downloads a Linux
rem  binary and cannot run here.
rem ============================================================
setlocal enabledelayedexpansion
cd /d "%~dp0"

set CFD=cloudflared
where cloudflared >nul 2>&1
if errorlevel 1 set CFD=C:\Program Files (x86)\cloudflared\cloudflared.exe
if not exist "%CFD%" if "%CFD%"=="cloudflared" goto have_cfd
if not exist "%CFD%" (
    echo ERROR: cloudflared not found.
    echo Install it:  winget install --id Cloudflare.cloudflared
    exit /b 1
)
:have_cfd

if /i "%~1"=="--stop" (
    echo Stopping tunnel...
    taskkill /im cloudflared.exe /f >nul 2>&1
    python publish_url.py --clear
    echo Done - the webapp will fall back to Hugging Face.
    exit /b 0
)

if not exist data\logs mkdir data\logs
set LOG=data\logs\tunnel.log
if exist "%LOG%" del /q "%LOG%"

rem Fail fast if there is nothing to expose: a tunnel to a dead port publishes
rem an address that 502s, which looks exactly like a broken model.
curl -s -o nul -m 4 http://127.0.0.1:8800/api/health
if errorlevel 1 (
    echo ERROR: nothing is answering on :8800 - start the model server first.
    exit /b 1
)

echo Starting tunnel to http://127.0.0.1:8800 ...
start "cloudflared" /min cmd /c ""%CFD%" tunnel --no-autoupdate --url http://127.0.0.1:8800 > "%LOG%" 2>&1"

set URL=
for /l %%i in (1,1,30) do (
    if "!URL!"=="" (
        for /f "tokens=*" %%u in ('findstr /r /c:"https://[a-z0-9-]*\.trycloudflare\.com" "%LOG%" 2^>nul') do (
            for %%t in (%%u) do (
                echo %%t | findstr /r /c:"https://.*\.trycloudflare\.com" >nul && set URL=%%t
            )
        )
        if "!URL!"=="" ping -n 2 127.0.0.1 >nul
    )
)

if "!URL!"=="" (
    echo ERROR: no address after 60s. See %LOG%
    exit /b 1
)

echo.
echo   Public address: !URL!
echo.
python publish_url.py !URL!
echo.
echo Leave the cloudflared window open. Stop with:  tunnel.bat --stop
endlocal
