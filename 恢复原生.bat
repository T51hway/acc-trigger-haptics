@echo off
cd /d "%~dp0"
title Restore native ACC rumble

echo.
echo  This renames the proxy DLL out of the way.
echo  ACC's own vibration comes back to normal.
echo.

set GAME=
for %%P in (
  "E:\SteamLibrary\steamapps\common\Assetto Corsa Competizione\AC2\Binaries\Win64"
  "D:\SteamLibrary\steamapps\common\Assetto Corsa Competizione\AC2\Binaries\Win64"
  "C:\SteamLibrary\steamapps\common\Assetto Corsa Competizione\AC2\Binaries\Win64"
  "C:\Program Files (x86)\Steam\steamapps\common\Assetto Corsa Competizione\AC2\Binaries\Win64"
) do (
  if exist "%%~P\AC2-Win64-Shipping.exe" if not defined GAME set GAME=%%~P
)

if not defined GAME (
  echo  [!] ACC not found. Edit this .bat and put your game path in the list.
  echo.
  pause
  exit /b 1
)
echo  Game folder: %GAME%
echo.

tasklist /FI "IMAGENAME eq AC2-Win64-Shipping.exe" 2>nul | find /I "AC2-Win64-Shipping.exe" >nul
if not errorlevel 1 (
  echo  [!] ACC is running. Close it first, then run this again.
  echo.
  pause
  exit /b 1
)

rem ---- restore through panel.py (same ownership checks as the panel) ----
set PYEXE=C:\Users\Tsihway\.workbuddy\binaries\python\versions\3.13.12\python.exe
if not exist "%PYEXE%" set PYEXE=C:\Users\Tsihway\AppData\Roaming\uv\python\cpython-3.13.3-windows-x86_64-none\python.exe
if not exist "%PYEXE%" set PYEXE=C:\Users\Tsihway\AppData\Roaming\uv\python\cpython-3.12.15-windows-x86_64-none\python.exe
if not exist "%PYEXE%" set PYEXE=python

"%PYEXE%" src\panel.py --restore "%GAME%"
if errorlevel 1 (
  echo  [!] Restore was refused or failed. Nothing was changed.
) else (
  echo  Done. ACC native rumble is back. Trigger rumble will be overwritten again.
)

echo.
pause
