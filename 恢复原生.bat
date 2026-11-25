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

if not exist "%GAME%\xinput1_3.dll" (
  echo  Not installed. Nothing to restore.
) else (
  if exist "%GAME%\xinput1_3.dll.off" del /q "%GAME%\xinput1_3.dll.off"
  ren "%GAME%\xinput1_3.dll" "xinput1_3.dll.off"
  if errorlevel 1 (
    echo  [!] Rename failed. Try running this bat as administrator.
  ) else (
    echo  Done. ACC native rumble is back. Trigger rumble will be overwritten again.
  )
)

echo.
pause
