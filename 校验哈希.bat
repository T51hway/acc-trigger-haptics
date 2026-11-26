@echo off
cd /d "%~dp0"
title Verify SHA256 - ACC Trigger Haptics

set "EXE=ACC-Trigger-Haptics.exe"
set "DLL=tools\xinput_proxy\xinput1_3.dll"
set "EXE_SHA=5088B2E8B6029664884F75A5F945F2C8A772FC2A2428DEBD9AA83282159871AA"
set "DLL_SHA=8E284775850C9CBA345B2B9912A71A45050F84C9186A0387387F5105CD4AFE6C"
set "FAIL=0"

echo.
echo  ACC Trigger Haptics - file check
echo  Comparing SHA256 fingerprints with the published values...
echo.

if exist "%EXE%" (
    call :check "%EXE%" "%EXE_SHA%"
) else (
    echo  [MISSING]  %EXE%
    set "FAIL=1"
)

if exist "%DLL%" (
    call :check "%DLL%" "%DLL_SHA%"
) else (
    echo  [SKIP]     xinput1_3.dll not found ^(release-only download^)
)

echo.
if "%FAIL%"=="0" (
    echo  Result: OK. The files that were checked match.
) else (
    echo  Result: MISMATCH or MISSING file.
    echo  Do not run it. Download again from the GitHub Releases page.
)
echo.
pause
exit /b 0

:check
setlocal
set "F=%~1"
set "EXPECT=%~2"
set "ACTUAL="
for /f "usebackq delims=" %%H in (`powershell -NoProfile -Command "(Get-FileHash -LiteralPath '%F%' -Algorithm SHA256).Hash"`) do set "ACTUAL=%%H"
if /i "%ACTUAL%"=="%EXPECT%" (
    echo  [OK]       %~nx1
    endlocal & exit /b
)
echo  [MISMATCH] %~nx1
echo             expected %EXPECT%
echo             actual   %ACTUAL%
endlocal & set "FAIL=1" & exit /b
