@echo off
setlocal EnableExtensions EnableDelayedExpansion
cd /d "%~dp0"
set "PROGRESS=%~dp0install-progress.txt"
set "BUILD_LOG=%~dp0build-install.log"

>"%BUILD_LOG%" echo ScoutBot Windows setup log
>>"%BUILD_LOG%" echo Started: %DATE% %TIME%

>"%PROGRESS%" echo 10^|Checking your Windows setup
where py >>"%BUILD_LOG%" 2>&1
if errorlevel 1 (
  >>"%BUILD_LOG%" echo ERROR: Python launcher was not found.
  echo.
  echo  ERROR: Python was not found.
  echo  INFO: ScoutBot needs Python 3.11 or newer to prepare the Windows app.
  exit /b 1
)

py -3 -c "import sys; assert sys.version_info >= (3,11), sys.version" >>"%BUILD_LOG%" 2>&1
if errorlevel 1 (
  >>"%BUILD_LOG%" echo ERROR: Python 3.11 or newer is required.
  echo.
  echo  ERROR: Python 3.11 or newer is required.
  echo  INFO: Install Python 3.11 or newer, then run install.bat again.
  exit /b 1
)

if not exist .venv (
  >"%PROGRESS%" echo 18^|Creating a private setup area for ScoutBot
  py -3 -m venv .venv >>"%BUILD_LOG%" 2>&1
  if errorlevel 1 (
    >>"%BUILD_LOG%" echo ERROR: Could not create the private Python setup area.
    exit /b 1
  )
)

>"%PROGRESS%" echo 28^|Getting the small Windows components ScoutBot needs
call .venv\Scripts\activate.bat >>"%BUILD_LOG%" 2>&1
if errorlevel 1 (
  >>"%BUILD_LOG%" echo ERROR: Could not activate the private Python setup area.
  exit /b 1
)
python -m pip install --upgrade pip --prefer-binary >>"%BUILD_LOG%" 2>&1
if errorlevel 1 (
  >>"%BUILD_LOG%" echo ERROR: Could not update the package installer.
  exit /b 1
)

>"%PROGRESS%" echo 48^|Installing ScoutBot's required components
python -m pip install -r scout-backend\requirements.txt pyinstaller^>=6.0 --prefer-binary >>"%BUILD_LOG%" 2>&1
if errorlevel 1 (
  >>"%BUILD_LOG%" echo ERROR: Required ScoutBot components could not be installed.
  exit /b 1
)

if exist build\work rmdir /s /q build\work >>"%BUILD_LOG%" 2>&1
if exist dist rmdir /s /q dist >>"%BUILD_LOG%" 2>&1
mkdir dist >>"%BUILD_LOG%" 2>&1

>"%PROGRESS%" echo 68^|Building the ScoutBot Windows application
python -m PyInstaller --noconfirm --clean build\scoutbot.spec >>"%BUILD_LOG%" 2>&1
if errorlevel 1 (
  >>"%BUILD_LOG%" echo ERROR: PyInstaller could not create the Windows application.
  exit /b 1
)

if not exist dist\ScoutBot.exe (
  >>"%BUILD_LOG%" echo ERROR: ScoutBot.exe was not created.
  exit /b 1
)

>"%PROGRESS%" echo 85^|Checking the finished ScoutBot application
for %%F in ("dist\ScoutBot.exe") do if %%~zF LSS 100000 (
  >>"%BUILD_LOG%" echo ERROR: ScoutBot.exe was unexpectedly small.
  exit /b 1
)

>"%PROGRESS%" echo 85^|ScoutBot is ready to be installed
>>"%BUILD_LOG%" echo Completed: %DATE% %TIME%
exit /b 0
