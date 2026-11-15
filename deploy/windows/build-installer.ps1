<#
.SYNOPSIS
Builds a self-contained Windows Desktop Installer (`dist\CompanionSetup-v3.4.0.exe`).
Downloads the official Python 3.12 Windows Embeddable Runtime (zero pip packages needed!),
stages the clean Companion app tree + silent Win32 tray launchers, and compiles `companion.iss`
if Inno Setup 6 (`ISCC.exe`) is installed.
#>
param(
    [string]$PythonVersion = "3.12.8"
)

$ErrorActionPreference = "Stop"
$RepoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path
$StageDir = Join-Path $RepoRoot "dist\stage"
$RuntimeDir = Join-Path $StageDir "runtime"

if (Test-Path $StageDir) { Remove-Item -Recurse -Force $StageDir }
New-Item -ItemType Directory -Force -Path $RuntimeDir | Out-Null

Write-Host "[1/4] Downloading official Python $PythonVersion embeddable zip (64-bit)..." -ForegroundColor Cyan
$ZipUrl = "https://www.python.org/ftp/python/$PythonVersion/python-$PythonVersion-embed-amd64.zip"
$ZipFile = Join-Path $RepoRoot "dist\python-embed.zip"
Invoke-WebRequest -Uri $ZipUrl -OutFile $ZipFile
Expand-Archive -Path $ZipFile -DestinationPath $RuntimeDir -Force
Remove-Item -Force $ZipFile

# Enable site/app root resolution in embedded Python ._pth file
$PthFile = Get-ChildItem -Path $RuntimeDir -Filter "python*._pth" | Select-Object -First 1
if ($PthFile) {
    @(
        "python312.zip",
        ".",
        "..",
        "import site"
    ) | Set-Content -Path $PthFile.FullName -Encoding ASCII
}

Write-Host "[2/4] Staging clean Companion AI OS application files (zero user data)..." -ForegroundColor Cyan
foreach ($Item in @("harness.py", ".env.example", "LICENSE", "README.md", "companion", "configs", "memory", "skills", "specs", "surfaces")) {
    $Src = Join-Path $RepoRoot $Item
    if (Test-Path $Src) {
        Copy-Item -Recurse -Force $Src (Join-Path $StageDir $Item)
    }
}
Get-ChildItem -Path $StageDir -Recurse -Include "__pycache__", "*.pyc", "*.db", "*.db-shm", "*.db-wal" -Force | Remove-Item -Recurse -Force -ErrorAction SilentlyContinue

Write-Host "[3/4] Writing windowless System Tray VBS launchers..." -ForegroundColor Cyan
$VbsInteractive = @'
Set sh = CreateObject("WScript.Shell")
Set fso = CreateObject("Scripting.FileSystemObject")
appDir = fso.GetParentFolderName(WScript.ScriptFullName)
dataDir = sh.ExpandEnvironmentStrings("%LOCALAPPDATA%\Companion\data")
cmd = """" & appDir & "\runtime\pythonw.exe"" """ & appDir & "\harness.py"" --data-dir """ & dataDir & """ --locked --tray"
sh.Run cmd, 0, False
'@
Set-Content -Path (Join-Path $StageDir "launch-companion.vbs") -Value $VbsInteractive -Encoding ASCII

$VbsSilent = $VbsInteractive.Replace("--tray", "--tray --no-browser")
Set-Content -Path (Join-Path $StageDir "launch-companion-silent.vbs") -Value $VbsSilent -Encoding ASCII

Write-Host "[4/4] Checking for Inno Setup 6 Compiler (ISCC.exe)..." -ForegroundColor Cyan
$IsccCandidates = @(
    (Get-Command ISCC.exe -ErrorAction SilentlyContinue).Source,
    "${env:ProgramFiles(x86)}\Inno Setup 6\ISCC.exe",
    "$env:ProgramFiles\Inno Setup 6\ISCC.exe",
    "$env:LOCALAPPDATA\Programs\Inno Setup 6\ISCC.exe"
) | Where-Object { $_ -and (Test-Path $_) }

if ($IsccCandidates.Count -gt 0) {
    $Iscc = $IsccCandidates[0]
    & $Iscc (Join-Path $PSScriptRoot "companion.iss")
    Write-Host "Built installer: $RepoRoot\dist\CompanionSetup-v3.4.0.exe" -ForegroundColor Green
} else {
    Write-Host "Portable bundle staged at: $StageDir" -ForegroundColor Yellow
    Write-Host "Install Inno Setup 6 (https://jrsoftware.org/isinfo.php) and re-run this script to compile CompanionSetup-v3.4.0.exe."
}
