param([string]$Root)
$ErrorActionPreference = 'Stop'

if ([string]::IsNullOrWhiteSpace($Root)) {
    $Root = $PSScriptRoot
}
$Root = [System.IO.Path]::GetFullPath($Root)
if (-not (Test-Path -LiteralPath $Root -PathType Container)) {
    throw "ScoutBot installation folder does not exist: $Root"
}
Set-Location -LiteralPath $Root

$progressFile = Join-Path $Root 'install-progress.txt'
$buildLog = Join-Path $Root 'build-install.log'
Remove-Item -LiteralPath $progressFile -Force -ErrorAction SilentlyContinue

function Emoji([int]$CodePoint) {
    return [char]::ConvertFromUtf32($CodePoint)
}

$eMouse = Emoji 0x1F42D
$eHello = Emoji 0x1F44B
$eBox = Emoji 0x1F4E6
$eLock = Emoji 0x1F512
$eSearch = Emoji 0x1F50E
$eTool = Emoji 0x1F6E0
$eCoffee = Emoji 0x2615
$eOk = Emoji 0x2705
$eRocket = Emoji 0x1F680
$eShield = Emoji 0x1F6E1
$eSave = Emoji 0x1F4BE
$eRefresh = Emoji 0x1F504
$eWarn = Emoji 0x26A0
$eError = Emoji 0x274C
$eDoc = Emoji 0x1F4CB
$eClock = Emoji 0x23F1

function Write-ProgressState([int]$Percent, [string]$Message) {
    "$Percent|$Message" | Set-Content -LiteralPath $progressFile -Encoding ASCII
}

function Format-Time([double]$Seconds) {
    if ($Seconds -lt 0 -or [double]::IsNaN($Seconds) -or [double]::IsInfinity($Seconds)) { return '--:--' }
    $ts = [TimeSpan]::FromSeconds([math]::Max(0, [math]::Round($Seconds)))
    if ($ts.TotalHours -ge 1) { return $ts.ToString('hh\:mm\:ss') }
    return $ts.ToString('mm\:ss')
}

$script:LastDrawLength = 0

function Draw([int]$Percent, [string]$Message, [double]$Elapsed, [double]$Eta) {
    # Keep the live frame on one physical console line. Long status text was
    # previously allowed to wrap, which left stale fragments such as
    # "tBot needs" visible after the next frame was drawn.
    $consoleWidth = 80
    try {
        if ([Console]::WindowWidth -gt 20) { $consoleWidth = [Console]::WindowWidth }
    } catch {
        $consoleWidth = 80
    }

    $width = 24
    $filled = [int][math]::Floor($width * $Percent / 100)
    if ($filled -lt 0) { $filled = 0 }
    if ($filled -gt $width) { $filled = $width }
    $bar = ('#' * $filled) + ('-' * ($width - $filled))

    $prefix = "  [$bar] {0,3}%  {1} {2}  ETA {3}  " -f $Percent, $eClock, (Format-Time $Elapsed), (Format-Time $Eta)
    $available = $consoleWidth - $prefix.Length - 1
    if ($available -lt 8) { $available = 8 }
    if ($Message.Length -gt $available) {
        $Message = $Message.Substring(0, $available)
    }

    $line = $prefix + $Message
    $clearLength = [math]::Max($script:LastDrawLength, $line.Length)
    if ($clearLength -gt 0) {
        Write-Host ("`r" + (' ' * $clearLength) + "`r") -NoNewline
    }
    Write-Host $line -NoNewline
    $script:LastDrawLength = $line.Length
}
Write-Host ''
Write-Host '============================================================' -ForegroundColor DarkGray
Write-Host ("  {0} ScoutBot installer" -f $eMouse) -ForegroundColor Cyan
Write-Host '============================================================' -ForegroundColor DarkGray
Write-Host ''
Write-Host ("  {0} ScoutBot will now be installed for this Windows user." -f $eHello)
Write-Host ("  {0} The installer will prepare the app, copy it into place," -f $eBox)
Write-Host '     and create easy Start Menu and Desktop shortcuts.'
Write-Host ("  {0} Your saved ScoutBot data is kept separately." -f $eLock)
Write-Host ''

$started = Get-Date
$exe = Join-Path $Root 'dist\ScoutBot.exe'
$buildNeeded = -not (Test-Path -LiteralPath $exe)

if ($buildNeeded) {
    Write-ProgressState 5 'Checking what ScoutBot needs'
    Write-Host ("  {0} The finished app is not included yet." -f $eSearch)
    Write-Host ("  {0} Windows will prepare it automatically now." -f $eTool)
    Write-Host ("  {0} This first setup can take a few minutes. Please leave this window open." -f $eCoffee)
    Write-Host ''

    $buildBat = Join-Path $Root 'build-windows.bat'
    $cmdExe = Join-Path $env:SystemRoot 'System32\cmd.exe'
    if (-not (Test-Path -LiteralPath $buildBat)) {
        Write-Host ("  {0} The Windows setup file is missing." -f $eError) -ForegroundColor Red
        Write-Host '  Please extract the complete ScoutBot folder and run install.bat again.'
        exit 1
    }
    Remove-Item -LiteralPath $buildLog -Force -ErrorAction SilentlyContinue
    $proc = Start-Process -FilePath $cmdExe -ArgumentList @('/d','/c', ('"{0}"' -f $buildBat)) -WorkingDirectory $Root -PassThru -WindowStyle Hidden
    $lastPercent = 5
    $lastMessage = 'Starting setup'
    while (-not $proc.HasExited) {
        if (Test-Path -LiteralPath $progressFile) {
            $raw = Get-Content -LiteralPath $progressFile -Raw -ErrorAction SilentlyContinue
            if ($raw) {
                $parts = $raw.Trim() -split '\|', 2
                if ($parts.Count -eq 2) {
                    $p = 0
                    if ([int]::TryParse($parts[0], [ref]$p)) {
                        $lastPercent = [math]::Max(5, [math]::Min(85, $p))
                        $lastMessage = $parts[1]
                    }
                }
            }
        }
        $elapsed = ((Get-Date) - $started).TotalSeconds
        if ($lastPercent -gt 0) { $eta = ($elapsed / $lastPercent) * (100 - $lastPercent) } else { $eta = 0 }
        Draw $lastPercent $lastMessage $elapsed $eta
        Start-Sleep -Milliseconds 500
    }
    if ($proc.ExitCode -ne 0) {
        Write-Host "`r" + (' ' * 120)
        Write-Host ''
        Write-Host ("  {0} ScoutBot could not be prepared." -f $eError) -ForegroundColor Red
        Write-Host '  Windows reported a setup error. Nothing was intentionally installed into the final app folder.'
        if (Test-Path -LiteralPath $buildLog) {
            Write-Host ("  {0} The detailed setup log is here:" -f $eDoc)
            Write-Host "     $buildLog"
            Write-Host ''
            Write-Host '  The last setup messages were:' -ForegroundColor Yellow
            Get-Content -LiteralPath $buildLog -Tail 20 -ErrorAction SilentlyContinue | ForEach-Object { Write-Host "     $_" }
        }
        Write-Host ''
        exit $proc.ExitCode
    }
}

Write-ProgressState 88 'Putting ScoutBot in its app folder'
$elapsed = ((Get-Date) - $started).TotalSeconds
Draw 88 'Putting ScoutBot in its app folder' $elapsed (($elapsed / 88) * 12)
Start-Sleep -Milliseconds 350

$installDir = Join-Path $env:LOCALAPPDATA 'Programs\ScoutBot'
$dataDir = Join-Path $env:LOCALAPPDATA 'ScoutBot\Data'
$existingExe = Join-Path $installDir 'ScoutBot.exe'
if (Test-Path -LiteralPath $existingExe) {
    Write-Host ("  {0} ScoutBot is already installed." -f $eRefresh)
    Write-Host '  This run will safely update or repair the app without removing your saved data.'
    Write-Host ''
    Get-Process ScoutBot -ErrorAction SilentlyContinue | Stop-Process -Force -ErrorAction SilentlyContinue
    $stopDeadline = (Get-Date).AddSeconds(10)
    while ((Get-Process ScoutBot -ErrorAction SilentlyContinue) -and (Get-Date) -lt $stopDeadline) {
        Start-Sleep -Milliseconds 250
    }
    if (Get-Process ScoutBot -ErrorAction SilentlyContinue) {
        throw 'ScoutBot is still running and Windows would not release the old application file.'
    }
}

New-Item -ItemType Directory -Force -Path $installDir | Out-Null
New-Item -ItemType Directory -Force -Path $dataDir | Out-Null
$backupDir = Join-Path $dataDir 'installer-backups'
New-Item -ItemType Directory -Force -Path $backupDir | Out-Null
$liveDb = Join-Path $dataDir 'streamers.db'
if (Test-Path -LiteralPath $liveDb) {
    $backupName = 'streamers-' + (Get-Date -Format 'yyyy-MM-dd-HHmmss') + '.db'
    $backupPath = Join-Path $backupDir $backupName
    Copy-Item -LiteralPath $liveDb -Destination $backupPath -Force
    foreach ($suffix in @("-wal", "-shm")) {
        if (Test-Path -LiteralPath ($liveDb + $suffix)) {
            Copy-Item -LiteralPath ($liveDb + $suffix) -Destination ($backupPath + $suffix) -Force
        }
    }
}
Copy-Item -LiteralPath $exe -Destination (Join-Path $installDir 'ScoutBot.exe') -Force
if (-not (Test-Path -LiteralPath (Join-Path $installDir 'ScoutBot.exe'))) { throw 'The ScoutBot application file could not be copied into place.' }
$uninstaller = Join-Path $Root 'uninstall.bat'
if (Test-Path -LiteralPath $uninstaller) { Copy-Item -LiteralPath $uninstaller -Destination (Join-Path $installDir 'Uninstall ScoutBot.bat') -Force }
$readme = Join-Path $Root 'README.md'
if (Test-Path -LiteralPath $readme) { Copy-Item -LiteralPath $readme -Destination (Join-Path $installDir 'README.md') -Force }

Write-ProgressState 93 'Creating your Desktop and Start Menu shortcuts'
$elapsed = ((Get-Date) - $started).TotalSeconds
Draw 93 'Creating your Desktop and Start Menu shortcuts' $elapsed (($elapsed / 93) * 7)

$ws = New-Object -ComObject WScript.Shell
$desktop = [Environment]::GetFolderPath('Desktop')
$start = Join-Path ([Environment]::GetFolderPath('StartMenu')) 'Programs'
$menu = Join-Path $start 'ScoutBot'
New-Item -ItemType Directory -Force -Path $menu | Out-Null

$target = Join-Path $installDir 'ScoutBot.exe'
$shortcut = $ws.CreateShortcut((Join-Path $desktop 'ScoutBot.lnk'))
$shortcut.TargetPath = $target
$shortcut.WorkingDirectory = $installDir
$shortcut.IconLocation = $target + ',0'
$shortcut.Save()

$startShortcut = $ws.CreateShortcut((Join-Path $menu 'ScoutBot.lnk'))
$startShortcut.TargetPath = $target
$startShortcut.WorkingDirectory = $installDir
$startShortcut.IconLocation = $target + ',0'
$startShortcut.Save()

$uninstallShortcut = $ws.CreateShortcut((Join-Path $menu 'Uninstall ScoutBot.lnk'))
$uninstallShortcut.TargetPath = Join-Path $installDir 'Uninstall ScoutBot.bat'
$uninstallShortcut.WorkingDirectory = $installDir
$uninstallShortcut.Save()

Write-ProgressState 98 'Finishing the installation'
$elapsed = ((Get-Date) - $started).TotalSeconds
Draw 98 'Finishing the installation' $elapsed (($elapsed / 98) * 2)
Start-Sleep -Milliseconds 350

Write-ProgressState 100 'ScoutBot is ready'
$elapsed = ((Get-Date) - $started).TotalSeconds
Draw 100 'ScoutBot is ready' $elapsed 0
Write-Host ''
Write-Host ''
Write-Host ("  {0} ScoutBot is installed." -f $eOk) -ForegroundColor Green
Write-Host ("  {0} A Desktop shortcut and Start Menu entry are ready." -f $eRocket)
Write-Host ("  {0} Your existing ScoutBot data was left in place. An installer safety copy was made before an update." -f $eShield)
Write-Host ("  {0} Your ScoutBot data will be kept here:" -f $eSave)
Write-Host "     $dataDir"
Write-Host ''
Write-Host '  Starting ScoutBot now...'
Write-Host ''
Start-Process -FilePath $target -WorkingDirectory $installDir
Remove-Item -LiteralPath $progressFile -Force -ErrorAction SilentlyContinue
