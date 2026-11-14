<#
.SYNOPSIS
Registers Companion AI OS to start automatically in the background (or System Tray) at Windows logon.
Isolates all user data cleanly under %LOCALAPPDATA%\Companion\data by default.
#>
param(
    [int]$Port = 8000,
    [ValidateSet("tray", "headless")]
    [string]$Mode = "tray",
    [string]$DataDir = "$env:LOCALAPPDATA\Companion\data"
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

$Flag = if ($Mode -eq "tray") { "--tray --no-browser --port $Port" } else { "--web --port $Port" }
$ArgsStr = "`"$RepoRoot\harness.py`" --data-dir `"$DataDir`" $Flag"

$Action = New-ScheduledTaskAction -Execute $PythonExe -Argument $ArgsStr -WorkingDirectory $RepoRoot
$Trigger = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME
$Settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -ExecutionTimeLimit ([TimeSpan]::Zero) -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 1)

Register-ScheduledTask -TaskName $TaskName -Action $Action -Trigger $Trigger -Settings $Settings -Description "Companion AI OS Background / System Tray Service" -Force | Out-Null
Start-ScheduledTask -TaskName $TaskName

Write-Host ""
Write-Host "Companion AI OS startup service registered and started!" -ForegroundColor Green
Write-Host "  Mode      : $Mode"
Write-Host "  Studio URL: http://127.0.0.1:$Port"
Write-Host "  User Data : $DataDir"
