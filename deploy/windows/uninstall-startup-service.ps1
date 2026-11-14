<#
.SYNOPSIS
Removes the Companion AI OS Windows startup scheduled task (leaving your user data folder completely untouched).
#>
$ErrorActionPreference = "SilentlyContinue"
$TaskName = "CompanionAIOS"

Stop-ScheduledTask -TaskName $TaskName
Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false

Write-Host "Removed Companion AI OS Windows startup task ('$TaskName'). Your user data folder was preserved." -ForegroundColor Yellow
