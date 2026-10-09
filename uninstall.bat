@echo off
setlocal EnableExtensions
set "INSTALL_DIR=%LOCALAPPDATA%\Programs\ScoutBot"
set "START_DIR=%APPDATA%\Microsoft\Windows\Start Menu\Programs\ScoutBot"
set "DESKTOP=%USERPROFILE%\Desktop\ScoutBot.lnk"
set "DATA_DIR=%LOCALAPPDATA%\ScoutBot\Data"
cd /d "%TEMP%"
powershell -NoProfile -ExecutionPolicy Bypass -Command "$ErrorActionPreference='Stop'; Get-Process ScoutBot -ErrorAction SilentlyContinue | Stop-Process -Force -ErrorAction SilentlyContinue; $shortcut=Join-Path ([Environment]::GetFolderPath('Desktop')) 'ScoutBot.lnk'; if(Test-Path -LiteralPath $shortcut){Remove-Item -LiteralPath $shortcut -Force}; if(Test-Path -LiteralPath $env:START_DIR){Remove-Item -LiteralPath $env:START_DIR -Recurse -Force}; Start-Sleep -Milliseconds 700; if(Test-Path -LiteralPath $env:INSTALL_DIR){Remove-Item -LiteralPath $env:INSTALL_DIR -Recurse -Force}"
if errorlevel 1 (
  echo ScoutBot could not be fully removed. Close ScoutBot and retry. Your data is preserved.
  exit /b 1
)
echo ScoutBot application removed.
echo Recovery data preserved at: %DATA_DIR%
exit /b 0
