[CmdletBinding()]
param(
    [switch]$AtStartup
)

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
$watchdog = Join-Path $root "scripts\watchdog.ps1"
if (-not (Test-Path -LiteralPath $watchdog)) {
    throw "Missing $watchdog"
}
$envFile = Join-Path $root ".env"
if (-not (Test-Path -LiteralPath $envFile)) {
    throw "Missing $envFile. Copy .env.example to .env and configure credentials first."
}

$action = New-ScheduledTaskAction `
    -Execute "PowerShell.exe" `
    -Argument "-NoProfile -WindowStyle Hidden -ExecutionPolicy Bypass -File `"$watchdog`""
$trigger = if ($AtStartup) {
    New-ScheduledTaskTrigger -AtStartup
} else {
    New-ScheduledTaskTrigger -AtLogOn
}
$settings = New-ScheduledTaskSettingsSet -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 1)

try {
    Register-ScheduledTask `
        -TaskName "AutoTrade-Watchdog" `
        -Action $action `
        -Trigger $trigger `
        -Settings $settings `
        -Description "Monitors AutoTrade daemon, account safety and data streams." `
        -RunLevel Limited `
        -Force `
        -ErrorAction Stop | Out-Null
    $triggerName = if ($AtStartup) { "startup" } else { "logon" }
    Write-Output "Registered AutoTrade-Watchdog ($triggerName trigger)"
} catch [System.UnauthorizedAccessException], [Microsoft.Management.Infrastructure.CimException] {
    $startup = [Environment]::GetFolderPath("Startup")
    if (-not $startup) { throw }
    $launcher = Join-Path $startup "AutoTrade-Watchdog.cmd"
    $content = "@echo off`r`nstart `"`" /min PowerShell.exe -NoProfile -WindowStyle Hidden -ExecutionPolicy Bypass -File `"$watchdog`"`r`n"
    Set-Content -LiteralPath $launcher -Value $content -Encoding ASCII
    Write-Output "Task Scheduler denied access; installed Startup launcher: $launcher"
}
