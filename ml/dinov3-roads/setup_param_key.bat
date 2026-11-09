@echo off
rem ============================================================
rem  Put an SSH key on PARAM Rudra. Run once; type the password
rem  one last time.
rem
rem  Worth the two minutes: you will be transferring checkpoints
rem  back and forth and watching squeue for days, and every one of
rem  those is otherwise a password. It also lets sftp run in true
rem  batch mode, which is what -b was designed for.
rem
rem  PARAM advertises `publickey` among its accepted methods, so
rem  this is allowed. Nothing secret leaves your machine: only the
rem  PUBLIC half of the key is uploaded. The private key stays in
rem  %USERPROFILE%\.ssh\id_ed25519 - do not copy it anywhere.
rem ============================================================
setlocal

set PUSER=vaibhavt_iitp
set PHOST=paramrudra.iitp.ac.in
set PPORT=4422
set KEY=%USERPROFILE%\.ssh\id_ed25519

if not exist "%USERPROFILE%\.ssh" mkdir "%USERPROFILE%\.ssh"

if exist "%KEY%" (
    echo   Key already exists: %KEY%
) else (
    echo   Generating an ed25519 key. Press Enter at BOTH prompts to
    echo   leave it without a passphrase, which is what makes the
    echo   transfers unattended.
    echo.
    ssh-keygen -t ed25519 -f "%KEY%" -C "%USERNAME%@urbanly"
    if errorlevel 1 (
        echo   ssh-keygen failed.
        pause
        exit /b 1
    )
)

if not exist "%KEY%.pub" (
    echo   FATAL: %KEY%.pub is missing - cannot install a key that is not there.
    pause
    exit /b 1
)

echo.
echo   Installing the PUBLIC key on %PUSER%@%PHOST%.
echo   Type your PARAM password when asked - the last time.
echo.

rem There is no ssh-copy-id on Windows, so do what it does: append the
rem public key to authorized_keys, creating it with the permissions
rem sshd insists on. Lustre honours these modes. sshd silently refuses
rem a key if ~/.ssh is group-writable, which is the usual reason this
rem "works" but still asks for a password.
type "%KEY%.pub" | ssh -p %PPORT% %PUSER%@%PHOST% "umask 077; mkdir -p ~/.ssh && cat >> ~/.ssh/authorized_keys && chmod 700 ~/.ssh && chmod 600 ~/.ssh/authorized_keys && echo INSTALLED"
if errorlevel 1 (
    echo.
    echo   Could not install the key. The password upload still works -
    echo   just run upload_to_param.bat and type the password.
    pause
    exit /b 1
)

echo.
echo   Testing it - this should NOT ask for a password:
echo.
ssh -p %PPORT% -o BatchMode=yes %PUSER%@%PHOST% "echo OK: key login works; hostname"
if errorlevel 1 (
    echo.
    echo   The key did not take. Not fatal - upload_to_param.bat will
    echo   fall back to asking for the password.
) else (
    echo.
    echo   Done. upload_to_param.bat will no longer ask for anything.
)
pause
