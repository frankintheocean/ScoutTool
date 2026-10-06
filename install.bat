@echo off
setlocal EnableExtensions

rem ScoutBot installer entry point.
rem Keep this file plain ASCII so Windows Command Prompt can read it reliably.
cd /d "%~dp0"

set "PS=%SystemRoot%\System32\WindowsPowerShell\v1.0\powershell.exe"
if not exist "%PS%" goto :powershell_missing

chcp 65001 >nul 2>&1
"%PS%" -NoLogo -NoProfile -NonInteractive -ExecutionPolicy Bypass -File "%~dp0install_progress.ps1" -Root "%~dp0."
set "EXITCODE=%ERRORLEVEL%"

if not "%EXITCODE%"=="0" goto :install_failed
exit /b 0

:powershell_missing
echo.
echo [ERROR] Windows PowerShell could not be found.
echo [INFO] ScoutBot needs the PowerShell that comes with Windows to finish installation.
echo [INFO] Nothing was changed. Please restore Windows PowerShell and run install.bat again.
echo.
pause
exit /b 1

:install_failed
echo.
echo [ERROR] ScoutBot could not finish installing.
echo [INFO] Nothing was intentionally removed. Fix the setup problem and run install.bat again.
echo [INFO] A setup log is available as build-install.log when the Windows app needed to be built.
echo.
pause
exit /b %EXITCODE%
