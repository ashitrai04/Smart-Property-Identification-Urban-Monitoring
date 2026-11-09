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
rem  PARAM asks for a CAPTCHA and THEN the password - see the user
rem  manual, page 15 ("Enter the captcha when prompted, then input
rem  your password") and Figure 7 on page 22. Both arrive as
rem  keyboard-interactive prompts, so expect two things to type, not
rem  one, and type the captcha exactly as shown including its case.
rem
rem  That captcha is why this has to be one session: every separate
rem  scp is another captcha. Here you answer it once and all four
rem  transfers follow.
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

rem `reput` resumes an upload; it CANNOT start one. It stats the remote
rem file to find where to continue from, so against a file that is not
rem there yet it fails with "stat remote: No such file or directory"
rem and transfers nothing. (Same for `put -a` - identical code path.)
rem So: `put` for a first run, `reput` only to pick up a broken one.
set XFER=put
set DATAFILES=1
if /i "%~1"=="resume" (
    set XFER=reput
    shift
)
rem "code" sends ONLY the scripts and skips the 5.4 GiB of data. Use it
rem after a code change: the data is already there, and re-putting it
rem would start again from byte zero.
if /i "%~1"=="code" (
    set DATAFILES=0
    shift
)

rem Optional overrides, so nothing here needs editing:
rem   upload_to_param.bat vaibhavt_iitp 4422
rem   upload_to_param.bat resume            <- continue a broken transfer
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
if "%DATAFILES%"=="0" goto checked
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

:checked

rem ---------- refuse to run from the wrong folder ----------
rem This uploads whatever sits in its OWN directory (%~dp0). Copy it somewhere
rem else - Downloads, the Desktop - and it will cheerfully upload that folder
rem instead, which is how one run ended with:
rem     lcd "C:/Users/asash/Downloads"
rem     put *.py
rem     stat *.py: No such file or directory
rem after the captcha and the password had already been typed. So check for a
rem file only the real code directory has, and fail before the prompts.
if not exist "%CODE%\slurm_train.sh" (
    echo.
    echo   FATAL: this is not the dinov3-roads directory.
    echo.
    echo     running from : %CODE%
    echo     expected     : the folder holding slurm_train.sh and train_roads.py
    echo.
    echo   This script uploads the folder it lives in, so a stray copy in
    echo   Downloads uploads Downloads. Delete the copy and run the original.
    echo.
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
rem A UNIQUE name per run. With a fixed one, a second run started while
rem the first is still going cannot write this file - sftp holds it open -
rem and every redirect below fails with "The process cannot access the
rem file because it is being used by another process". The old batch
rem would then be left on disk and handed to sftp: the second session
rem would re-run the FIRST session's transfer list, writing the same
rem 5.4 GiB remote file the first session is still writing. That is how
rem you get a corrupt archive that only fails at sha256sum, hours later.
set "BATCH=%TEMP%\param_upload.%RANDOM%%RANDOM%.sftp"
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
if "%DATAFILES%"=="0" goto no_data
>>"%BATCH%" echo lcd "%UPLOADF%"
>>"%BATCH%" echo cd /scratch/%PUSER%
>>"%BATCH%" echo %XFER% ongole_roads_lean.tar
>>"%BATCH%" echo put ongole_roads_lean.tar.sha256
rem --- inference clip and labels ---
>>"%BATCH%" echo cd /scratch/%PUSER%/data
>>"%BATCH%" echo %XFER% ongole_1km.tif
>>"%BATCH%" echo put "%LABELS%"
:no_data
rem (the "bye" line is appended just below, then the file is checked)
>>"%BATCH%" echo bye

rem Never hand sftp a batch file we did not just write in full.
if not exist "%BATCH%" (
    echo   FATAL: could not write %BATCH%
    echo   Nothing was uploaded.
    pause
    exit /b 1
)
rem A size test, not `find` or `findstr`: run this from a shell whose PATH
rem carries Unix tools - Git Bash, MSYS, WSL interop - and cmd resolves `find`
rem to /usr/bin/find, which takes different arguments and sits waiting on
rem stdin forever. A for-loop size test spawns no external command at all.
set BSIZE=0
for %%A in ("%BATCH%") do set BSIZE=%%~zA
if %BSIZE% LSS 40 (
    echo   FATAL: %BATCH% is empty or incomplete ^(%BSIZE% bytes^).
    echo   Refusing to run sftp against it - a stale or partial command list
    echo   could transfer the wrong thing.
    del "%BATCH%" 2>nul
    pause
    exit /b 1
)

echo.
echo ============================================================
echo   Uploading to %PUSER%@%PHOST%:%PPORT%
echo.
echo   code     %CODE%
echo   chips    %UPLOAD%\ongole_roads_lean.tar   (5.4 GiB)
echo   clip     %UPLOAD%\ongole_1km.tif          (235 MB)
echo   labels   %LABELS%
echo.
echo   You will be asked for a CAPTCHA first, then your password.
echo   Type the captcha exactly as shown, case included.
echo.
echo   One challenge for all four transfers.
echo.
echo   mode: %XFER%  data=%DATAFILES%
echo     upload_to_param.bat           everything, from the start
echo     upload_to_param.bat resume    continue a broken big file
echo     upload_to_param.bat code      scripts only, skip the 5.4 GiB
echo.
echo   Several "remote mkdir ... Failure" lines at the start are
echo   EXPECTED and harmless - they mean the directory already exists.
echo.
echo   If no captcha prompt appears and it fails straight away, use
echo   WinSCP instead - the manual recommends it, page 21. Protocol
echo   SFTP, host paramrudra.iitp.ac.in, PORT 4422 not 22.
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
    echo.
    if "%RC%"=="255" (
        echo   255 is an authentication or connection failure, not a
        echo   transfer failure. If you were never shown a captcha, this
        echo   build of sftp will not do keyboard-interactive auth here;
        echo   use WinSCP ^(manual page 21^) with protocol SFTP, host
        echo   paramrudra.iitp.ac.in, port 4422. It holds one session
        echo   open, so one captcha covers every file, and it resumes a
        echo   broken transfer.
        echo.
    )
    echo   If it broke part way through the big file, resume it with:
    echo       upload_to_param.bat resume
    echo   Starting over from scratch is just: upload_to_param.bat
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
