$ErrorActionPreference = 'Stop'
python -m compileall -q .\scout-backend .\standalone_launcher.py
if ($LASTEXITCODE -ne 0) { throw 'Python syntax validation failed' }
if (!(Test-Path .\build\scoutbot.spec)) { throw 'Missing PyInstaller spec' }
if (!(Test-Path .\install.bat)) { throw 'Missing installer' }
if (!(Test-Path .\uninstall.bat)) { throw 'Missing uninstaller' }
if (!(Test-Path .\frontend\index.html)) { throw 'Missing Scout frontend' }
Write-Host 'PASS: ScoutBot source, Python syntax, build spec, installer and uninstaller validated.'
