@echo off
REM ============================================================
REM  Right-click this file -> Run as administrator.  Once per laptop.
REM
REM  Windows puts the Tailscale adapter in the Private network profile,
REM  while the firewall rule Python gets from the prompt someone clicked
REM  through covers Public only. The effect is a laptop that can reach the
REM  other one but cannot be reached by it - which from the other side is
REM  indistinguishable from this machine being switched off, and shows up
REM  as "offline" in a sync that then pulls nothing.
REM
REM  The ports are opened to 100.64.0.0/10 - the range Tailscale hands out
REM  and nothing else. Your wifi network, and any cafe network you join
REM  later, still cannot reach them.
REM
REM    8777  data server    imagery and layers, for the webapp and QGIS
REM    8790  node service   file sync between the laptops
REM    8800  model server   HiSup, SegFormer, potholes
REM    8900  height server  DSM/DTM building heights
REM ============================================================
title Urbanly - allow the tailnet through the firewall

net session >nul 2>nul
if errorlevel 1 (
  echo.
  echo   [!] This needs administrator rights.
  echo       Close this window, right-click "ALLOW TAILSCALE.bat"
  echo       and choose "Run as administrator".
  echo.
  pause
  exit /b 1
)

echo.
echo   Opening 8777, 8790, 8800 and 8900 to the tailnet...

netsh advfirewall firewall delete rule name="Urbanly (Tailscale)" >nul 2>nul
netsh advfirewall firewall add rule name="Urbanly (Tailscale)" ^
  dir=in action=allow protocol=TCP ^
  localport=8777,8790,8800,8900 ^
  remoteip=100.64.0.0/10 profile=any
if errorlevel 1 (
  echo.
  echo   [!] The rule could not be added. The message above says why.
  echo.
  pause
  exit /b 1
)

echo.
echo   Done. The other laptop can now reach this one.
echo   To undo it:  netsh advfirewall firewall delete rule name="Urbanly (Tailscale)"
echo.
pause
