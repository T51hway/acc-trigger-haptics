@echo off
cd /d "%~dp0"
title Enable trigger rumble takeover

echo.
echo  This copies a small proxy DLL into ACC's binary folder.
echo  Effect: ACC can no longer send rumble to the controller,
echo          so our trigger vibration stops being overwritten.
echo.
echo  Reading the controller still works normally.
echo  To undo: run the other bat (restore native rumble).
echo.

rem ---- locate the game ----
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

rem ---- make sure the game is closed ----
tasklist /FI "IMAGENAME eq AC2-Win64-Shipping.exe" 2>nul | find /I "AC2-Win64-Shipping.exe" >nul
if not errorlevel 1 (
  echo  [!] ACC is running. Close it first, then run this again.
  echo.
  pause
  exit /b 1
)

rem ---- install through panel.py (same ownership checks as the panel) ----
if not exist "tools\xinput_proxy\xinput1_3.dll" (
  echo  [!] tools\xinput_proxy\xinput1_3.dll is missing.
  echo.
  pause
  exit /b 1
)

set PYEXE=C:\Users\Tsihway\.workbuddy\binaries\python\versions\3.13.12\python.exe
if not exist "%PYEXE%" set PYEXE=C:\Users\Tsihway\AppData\Roaming\uv\python\cpython-3.13.3-windows-x86_64-none\python.exe
if not exist "%PYEXE%" set PYEXE=C:\Users\Tsihway\AppData\Roaming\uv\python\cpython-3.12.15-windows-x86_64-none\python.exe
if not exist "%PYEXE%" set PYEXE=python

"%PYEXE%" src\panel.py --install "%GAME%"
if errorlevel 1 (
  echo  [!] Install was refused or failed. Nothing was changed.
) else (
  echo  Done. ACC rumble is now blocked; trigger rumble is ours alone.
)

echo.
pause
