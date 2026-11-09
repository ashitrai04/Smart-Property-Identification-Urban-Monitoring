@echo off
rem ============================================================
rem  Upload the chip set, the inference clip, the labels and this
rem  code to PARAM Rudra. Run it from cmd.exe or by double-click.
rem
rem  Why sftp and not scp or rsync:
rem    - rsync is not installed on Windows and Git for Windows does
rem      not ship it, so "rsync -avP" simply is not available here.
rem    - scp cannot resume. A dropped link 4 GB into a 5.4 GB file
rem      means starting over.
rem    - sftp's `reput` resumes from wherever it stopped, and one
rem      batch session means ONE password prompt for everything.
rem
rem  Safe to re-run. reput continues a partial file, and the
rem  directory creations are prefixed `-` so an existing directory
rem  is not an error.
rem ============================================================
setlocal

set PUSER=vaibhavt_iitp
set PHOST=paramrudra.iitp.ac.in
set PPORT=4422
set UPLOAD=C:\param_upload

rem Optional overrides, so nothing here needs editing:
rem   upload_to_param.bat vaibhavt_iitp 4422
if not "%~1"=="" set PUSER=%~1
if not "%~2"=="" set PPORT=%~2
if not "%~3"=="" set PHOST=%~3

set "CODE=%~dp0"
if "%CODE:~-1%"=="\" set "CODE=%CODE:~0,-1%"
rem sftp reads a backslash as an escape character, so lcd gets forward slashes.
set "CODEF=%CODE:\=/%"
set "UPLOADF=%UPLOAD:\=/%"
set LABELS=C:/Users/asash/Downloads/centre_part_Rd_v1.zip

rem ---------- check locally before asking for a password ----------
set MISSING=0
for %%F in ("%UPLOAD%\ongole_roads_lean.tar" "%UPLOAD%\ongole_roads_lean.tar.sha256" "%UPLOAD%\ongole_1km.tif" "%LABELS%") do (
    if not exist %%F (
        echo   MISSING: %%F
        set MISSING=1
    )
)
if "%MISSING%"=="1" (
    echo.
    echo   Nothing was uploaded. Fix the paths above and re-run.
    echo   The chip tar is built by:  bash package_for_param.sh
    pause
    exit /b 1
)

where sftp >nul 2>nul
if errorlevel 1 (
    echo   FATAL: no sftp on PATH. Install the Windows OpenSSH client:
    echo     Settings  ^>  System  ^>  Optional features  ^>  OpenSSH Client
    pause
    exit /b 1
)

rem ---------- the batch script sftp will run ----------
rem A leading `-` on a line means "carry on if this fails", which is
rem what we want for mkdir on a directory that already exists.
set BATCH=%TEMP%\param_upload.sftp
> "%BATCH%" echo progress
>>"%BATCH%" echo -mkdir /scratch/%PUSER%
>>"%BATCH%" echo -mkdir /scratch/%PUSER%/data
>>"%BATCH%" echo -mkdir /scratch/%PUSER%/hf
>>"%BATCH%" echo -mkdir /scratch/%PUSER%/runs
>>"%BATCH%" echo -mkdir /home/%PUSER%/dinov3-roads
>>"%BATCH%" echo -mkdir /home/%PUSER%/dinov3-roads/logs
rem --- code: lcd first so the globs below carry no spaces ---
>>"%BATCH%" echo lcd "%CODEF%"
>>"%BATCH%" echo cd /home/%PUSER%/dinov3-roads
>>"%BATCH%" echo put *.py
>>"%BATCH%" echo put *.sh
>>"%BATCH%" echo put *.yaml
>>"%BATCH%" echo put *.md
rem --- the chips: reput, so a broken transfer resumes ---
>>"%BATCH%" echo lcd "%UPLOADF%"
>>"%BATCH%" echo cd /scratch/%PUSER%
>>"%BATCH%" echo reput ongole_roads_lean.tar
>>"%BATCH%" echo reput ongole_roads_lean.tar.sha256
rem --- inference clip and labels ---
>>"%BATCH%" echo cd /scratch/%PUSER%/data
>>"%BATCH%" echo reput ongole_1km.tif
>>"%BATCH%" echo put "%LABELS%"
>>"%BATCH%" echo bye

echo.
echo ============================================================
echo   Uploading to %PUSER%@%PHOST%:%PPORT%
echo.
echo   code     %CODE%
echo   chips    %UPLOAD%\ongole_roads_lean.tar   (5.4 GiB)
echo   clip     %UPLOAD%\ongole_1km.tif          (235 MB)
echo   labels   %LABELS%
echo.
echo   One password prompt. The big file resumes if the link drops -
echo   just run this again.
echo.
echo   Tired of typing the password? Run setup_param_key.bat once.
echo ============================================================
echo.

rem -o BatchMode=no MUST come before -b. `-b` appends "-obatchmode yes"
rem to the ssh arguments, and ssh takes the FIRST value it is given for
rem any option - so an override has to be on the command line earlier
rem than the -b that causes it. With batchmode on, ssh is forbidden from
rem prompting, so a password login dies as "Permission denied" without
rem ever asking for the password. That is what happened the first time.
sftp -P %PPORT% -o BatchMode=no -b "%BATCH%" %PUSER%@%PHOST%
set RC=%ERRORLEVEL%
del "%BATCH%" 2>nul

echo.
if not "%RC%"=="0" (
    echo   sftp exited %RC% - the transfer did not finish.
    echo   Re-run this file: reput picks up where it stopped.
    pause
    exit /b %RC%
)

echo   Upload finished. Now, on PARAM:
echo.
echo     cd /scratch/$USER
echo     sha256sum -c ongole_roads_lean.tar.sha256
echo     tar -xf ongole_roads_lean.tar
echo     unzip -o data/centre_part_Rd_v1.zip -d data/
echo     find ongole_roads_v2 -type f ^| wc -l      # expect 35469
echo.
echo   Check the sha256 says OK before you extract.
pause
