<#
.SYNOPSIS
Registers Companion AI OS to start automatically in the background (or System Tray) at Windows logon.
Isolates all user data cleanly under %LOCALAPPDATA%\Companion\data by default.
#>
param(
    [int]$Port = 8000,
    [ValidateSet("tray", "headless")]
    [string]$Mode = "tray",
    [string]$DataDir = "$env:LOCALAPPDATA\Companion\data",
    [switch]$StopExisting
)

$ErrorActionPreference = "Stop"
$RepoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path
$TaskName = "CompanionAIOS"

$PythonExe = (Get-Command pythonw.exe -ErrorAction SilentlyContinue).Source
if (-not $PythonExe) {
    $PythonExe = (Get-Command python.exe -ErrorAction Stop).Source
}

New-Item -ItemType Directory -Force -Path $DataDir | Out-Null
$EnvTarget = Join-Path $DataDir ".env"
$EnvTemplate = Join-Path $RepoRoot ".env.example"
if ((-not (Test-Path $EnvTarget)) -and (Test-Path $EnvTemplate)) {
    Copy-Item $EnvTemplate $EnvTarget
}

Stop-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
$ExistingPids = @(Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue | Select-Object -ExpandProperty OwningProcess -Unique | Where-Object { $_ -gt 0 })
if ($ExistingPids.Count -gt 0 -and $StopExisting) {
    $VerifiedCompanion = $false
    try {
        $Health = Invoke-RestMethod -Uri "http://127.0.0.1:$Port/api/health" -Method Get -TimeoutSec 2 -ErrorAction Stop
        if ($Health -and $Health.service -like "companion-web*") {
            $VerifiedCompanion = $true
        }
    } catch {
        $VerifiedCompanion = $false
    }
    if (-not $VerifiedCompanion) {
        throw "Refusing to stop PID(s) $($ExistingPids -join ', ') on port $Port because /api/health did not identify a Companion Web server."
    }
    foreach ($PidToStop in $ExistingPids) {
        Write-Host "Stopping existing Companion server on port $Port (PID $PidToStop)..." -ForegroundColor Yellow
        Stop-Process -Id $PidToStop -Force -ErrorAction SilentlyContinue
    }
    Start-Sleep -Milliseconds 600
    $ExistingPids = @()
}

$Flag = if ($Mode -eq "tray") { "--immutable --tray --no-browser --host 127.0.0.1 --port $Port" } else { "--immutable --web --host 127.0.0.1 --port $Port" }
$ArgsStr = "`"$RepoRoot\harness.py`" --data-dir `"$DataDir`" $Flag"

$Action = New-ScheduledTaskAction -Execute $PythonExe -Argument $ArgsStr -WorkingDirectory $RepoRoot
$Trigger = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME
$Principal = New-ScheduledTaskPrincipal -UserId $env:USERNAME -LogonType Interactive
$Settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -ExecutionTimeLimit ([TimeSpan]::Zero) -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 1)

Register-ScheduledTask -TaskName $TaskName -Action $Action -Trigger $Trigger -Principal $Principal -Settings $Settings -Description "Companion AI OS Background / System Tray Service" -Force | Out-Null

if ($ExistingPids.Count -gt 0) {
    Write-Host ""
    Write-Host "Companion AI OS startup service registered for Windows logon!" -ForegroundColor Green
    Write-Host "  WARNING   : Port $Port is currently in use by an existing process (PID $($ExistingPids -join ', ')), such as a terminal '--web' session." -ForegroundColor Yellow
    Write-Host "  NEXT STEP : Close that terminal window (or re-run with '-StopExisting') so the System Tray icon service can take over port $Port!" -ForegroundColor Yellow
    Write-Host "  User Data : $DataDir"
} else {
    Start-ScheduledTask -TaskName $TaskName
    Write-Host ""
    Write-Host "Companion AI OS startup service registered and started!" -ForegroundColor Green
    Write-Host "  Mode      : $Mode"
    Write-Host "  Tray Icon : Check the '^' (Show hidden icons) arrow in your bottom-right taskbar (drag to pin!)"
    Write-Host "  Studio URL: http://127.0.0.1:$Port"
    Write-Host "  User Data : $DataDir"
}
