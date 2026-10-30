@echo off
rem ============================================================
rem  Swap in a newer building model.
rem
rem    update_building_model.bat D:\NHAI_DATA\hisup_weights\px512_e140.pth
rem
rem  Copies the checkpoint over weights\hisup_ongole.pth, keeping the old one
rem  as .prev so a bad swap is one rename away from undone, then tells a
rem  running server to drop its cached model. The next request loads the new
rem  weights. Nothing restarts and no deploy is needed - the webapp talks to
rem  whatever the server currently holds.
rem
rem  Training produces a better checkpoint every day or two, so this is the
rem  whole update procedure.
rem ============================================================
setlocal
cd /d "%~dp0"

if "%~1"=="" (
    echo Usage: update_building_model.bat ^<path-to-checkpoint.pth^>
    echo.
    echo Current:
    if exist weights\hisup_ongole.pth (
        for %%f in (weights\hisup_ongole.pth) do echo    %%~zf bytes, modified %%~tf
    ) else (
        echo    none installed
    )
    exit /b 1
)

if not exist "%~1" (
    echo ERROR: no such file: %~1
    exit /b 1
)

echo Checking the checkpoint before installing it...
python -c "import sys,torch; c=torch.load(sys.argv[1],map_location='cpu',weights_only=False); sd=c.get('model',c.get('state_dict',c)); n=len(sd); print(f'   {n} tensors'); sys.exit(0 if n>1500 else 1)" "%~1"
if errorlevel 1 (
    echo ERROR: that does not look like a HiSup checkpoint - not installing.
    exit /b 1
)

if exist weights\hisup_ongole.pth (
    echo Keeping the current one as weights\hisup_ongole.prev
    if exist weights\hisup_ongole.prev del /q weights\hisup_ongole.prev
    ren weights\hisup_ongole.pth hisup_ongole.prev
)

echo Installing...
copy /y "%~1" weights\hisup_ongole.pth >nul
if errorlevel 1 (
    echo ERROR: copy failed - restoring the previous model
    if exist weights\hisup_ongole.prev ren weights\hisup_ongole.prev hisup_ongole.pth
    exit /b 1
)

echo Telling the server to reload...
curl -s -X POST -m 10 http://127.0.0.1:8800/hisup/reload
echo.
curl -s -m 10 http://127.0.0.1:8800/hisup/health
echo.
echo.
echo Done. The next request uses the new model.
echo To undo:  ren weights\hisup_ongole.prev hisup_ongole.pth  ^&^& curl -X POST http://127.0.0.1:8800/hisup/reload
endlocal
