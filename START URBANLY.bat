@echo off
REM Double-click this to start Urbanly. Everything else is worked out for you.
REM The full path is spelled out because cmd does not reliably resolve a bare
REM script name from the directory it was just moved to.
cd /d "%~dp0urbanly-node"
call "%~dp0urbanly-node\start.bat" %*
