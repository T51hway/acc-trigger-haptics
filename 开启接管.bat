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

rem ---- install ----
if not exist "tools\xinput_proxy\xinput1_3.dll" (
  echo  [!] tools\xinput_proxy\xinput1_3.dll is missing.
  echo.
  pause
  exit /b 1
)

if exist "%GAME%\xinput1_3.dll.off" del /q "%GAME%\xinput1_3.dll.off"
copy /Y "tools\xinput_proxy\xinput1_3.dll" "%GAME%\xinput1_3.dll" >nul
if errorlevel 1 (
  echo  [!] Copy failed. Try running this bat as administrator.
) else (
  echo  Done. ACC rumble is now blocked; trigger rumble is ours alone.
)

echo.
pause
