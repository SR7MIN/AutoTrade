[CmdletBinding()]
param(
    [int]$IntervalSeconds = 60,
    [switch]$Once
)

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
Set-Location $root

# Load simple KEY=VALUE entries without printing secrets or overriding process values.
$envFile = Join-Path $root ".env"
if (Test-Path -LiteralPath $envFile) {
    foreach ($line in (Get-Content -LiteralPath $envFile -Encoding UTF8)) {
        $trimmed = $line.Trim()
        if (-not $trimmed -or $trimmed.StartsWith("#") -or $trimmed -notmatch "=") { continue }
        $parts = $trimmed.Split("=", 2)
        $key = $parts[0].Trim()
        $value = $parts[1].Trim().Trim('"').Trim("'")
        if ($key -and -not (Get-Item -Path "Env:$key" -ErrorAction SilentlyContinue)) {
            Set-Item -Path "Env:$key" -Value $value
        }
    }
}
if (-not $PSBoundParameters.ContainsKey("IntervalSeconds") -and
    $env:AUTOTRADE_WATCHDOG_INTERVAL_SECONDS) {
    $IntervalSeconds = [int]$env:AUTOTRADE_WATCHDOG_INTERVAL_SECONDS
}

$exe = Join-Path $root ".venv\Scripts\autotrade.exe"
$statePath = Join-Path $root ".autotrade\watchdog-state.json"
$logPath = Join-Path $root ".autotrade\watchdog.jsonl"
$watchdogLockPath = Join-Path $root ".autotrade\watchdog.lock"
$webhook = $env:AUTOTRADE_ALERT_WEBHOOK
$webhookFormat = if ($env:AUTOTRADE_ALERT_WEBHOOK_FORMAT) {
    $env:AUTOTRADE_ALERT_WEBHOOK_FORMAT.ToLowerInvariant()
} else {
    "generic"
}
$expectedEnvironment = if ($env:AUTOTRADE_WATCHDOG_EXPECTED_ENV) {
    $env:AUTOTRADE_WATCHDOG_EXPECTED_ENV.ToLowerInvariant()
} else {
    "testnet"
}
$expectEntryPaused = $env:AUTOTRADE_WATCHDOG_EXPECT_ENTRY_PAUSED -eq "true"

if (-not (Test-Path -LiteralPath $exe)) {
    throw "Missing $exe"
}
New-Item -ItemType Directory -Force -Path (Split-Path -Parent $statePath) | Out-Null

if (Test-Path -LiteralPath $watchdogLockPath) {
    try {
        $existingLock = Get-Content -LiteralPath $watchdogLockPath -Raw -Encoding UTF8 | ConvertFrom-Json
        if (Get-Process -Id ([int]$existingLock.pid) -ErrorAction SilentlyContinue) {
            throw "Another watchdog is already running with PID $($existingLock.pid)"
        }
    } catch {
        if ($_.Exception.Message -like "Another watchdog is already running*") { throw }
    }
}
(@{ pid = $PID; started_at = (Get-Date).ToUniversalTime().ToString("o") } | ConvertTo-Json) |
    Set-Content -LiteralPath $watchdogLockPath -Encoding UTF8

function Write-WatchdogLog {
    param([hashtable]$Record)
    ($Record | ConvertTo-Json -Compress -Depth 8) | Add-Content -LiteralPath $logPath -Encoding UTF8
}

function Invoke-AutoTradeJson {
    param([string[]]$Arguments)
    $raw = (& $exe @Arguments 2>&1 | Out-String).Trim()
    $code = $LASTEXITCODE
    if ($code -ne 0) {
        throw "autotrade $($Arguments -join ' ') failed with exit code ${code}: $raw"
    }
    try {
        return ($raw | ConvertFrom-Json)
    } catch {
        throw "autotrade $($Arguments -join ' ') returned invalid JSON: $raw"
    }
}

function Get-DaemonLockStatus {
    $lockPath = Join-Path $root ".autotrade\writer.lock"
    if (-not (Test-Path -LiteralPath $lockPath)) {
        return [pscustomobject]@{ Ok = $false; Reason = "writer.lock is missing" }
    }
    try {
        $payload = Get-Content -LiteralPath $lockPath -Raw -Encoding UTF8 | ConvertFrom-Json
        $process = Get-Process -Id ([int]$payload.pid) -ErrorAction SilentlyContinue
        if (-not $process) {
            return [pscustomobject]@{ Ok = $false; Reason = "writer.lock PID is not running" }
        }
        return [pscustomobject]@{ Ok = $true; Reason = "daemon PID $($payload.pid) is running" }
    } catch {
        return [pscustomobject]@{ Ok = $false; Reason = "writer.lock is invalid: $($_.Exception.Message)" }
    }
}

function Get-Issues {
    param(
        $Snapshot,
        $Health,
        $LockStatus,
        [string[]]$CommandErrors
    )
    $issues = @()
    if (-not $LockStatus.Ok) {
        $issues += [pscustomobject]@{ Severity = "CRITICAL"; Code = "DAEMON_DOWN"; Message = $LockStatus.Reason }
    }
    foreach ($errorText in $CommandErrors) {
        $issues += [pscustomobject]@{ Severity = "CRITICAL"; Code = "COMMAND_FAILED"; Message = $errorText }
    }
    if ($Health -and $Health.environment -ne $expectedEnvironment) {
        $issues += [pscustomobject]@{
            Severity = "CRITICAL"; Code = "ENVIRONMENT_MISMATCH"
            Message = "Expected $expectedEnvironment but got $($Health.environment)"
        }
    }
    if (-not $Snapshot) {
        return $issues
    }

    $controls = @{}
    foreach ($property in $Snapshot.controls.PSObject.Properties) {
        $controls[$property.Name] = $property.Value
    }
    if (-not $controls.ContainsKey("user_stream_healthy") -or
        $controls["user_stream_healthy"].value -ne "true") {
        $issues += [pscustomobject]@{ Severity = "CRITICAL"; Code = "USER_STREAM_UNHEALTHY"; Message = "user_stream_healthy is not true" }
    }
    foreach ($property in $Snapshot.controls.PSObject.Properties) {
        if ($property.Name -like "market_data_*_healthy" -and $property.Value.value -ne "true") {
            $issues += [pscustomobject]@{ Severity = "ERROR"; Code = "MARKET_DATA_UNHEALTHY"; Message = "$($property.Name) is not true" }
        }
    }
    if ($expectEntryPaused -and $controls.ContainsKey("entry_enabled") -and
        $controls["entry_enabled"].value -ne "false") {
        $issues += [pscustomobject]@{ Severity = "CRITICAL"; Code = "ENTRY_UNEXPECTEDLY_ENABLED"; Message = "entry_enabled is true while watchdog requires pause" }
    }

    $activeOrders = @($Snapshot.activeLocalOrders)
    foreach ($position in @($Snapshot.exchange.positions)) {
        if ([decimal]$position.positionAmt -eq 0) { continue }
        $symbol = [string]$position.symbol
        $symbolOrders = @($activeOrders | Where-Object { $_.symbol -eq $symbol })
        $stops = @($symbolOrders | Where-Object { $_.role -eq "STOP" -and $_.status -notin @("FILLED", "CANCELED", "EXPIRED", "REJECTED", "FINISHED") })
        if ($stops.Count -eq 0) {
            $issues += [pscustomobject]@{ Severity = "CRITICAL"; Code = "UNPROTECTED_POSITION"; Message = "$symbol has a position but no active local STOP order" }
        }
        $takes = @($symbolOrders | Where-Object { $_.role -eq "TAKE_PROFIT" -and $_.status -notin @("FILLED", "CANCELED", "EXPIRED", "REJECTED", "FINISHED") })
        if ($takes.Count -eq 0) {
            $issues += [pscustomobject]@{ Severity = "WARNING"; Code = "MISSING_TAKE_PROFIT"; Message = "$symbol has a position but no active local TAKE_PROFIT order" }
        }
    }

    $rates = $Snapshot.rateLimits
    if ($rates.weight_limit_1m -and ([decimal]$rates.request_weight_1m / [decimal]$rates.weight_limit_1m) -ge 0.85) {
        $issues += [pscustomobject]@{ Severity = "WARNING"; Code = "REQUEST_WEIGHT_HIGH"; Message = "1m request-weight utilization is at least 85%" }
    }
    return $issues
}

function Send-Alert {
    param(
        [string]$Event,
        [string]$Severity,
        [string]$Message,
        [object[]]$Issues
    )
    $record = @{
        time = (Get-Date).ToUniversalTime().ToString("o")
        event = $Event
        severity = $Severity
        environment = $expectedEnvironment
        message = $Message
        issues = @($Issues)
    }
    Write-WatchdogLog $record
    if (-not $webhook) { return }

    if ($webhookFormat -eq "feishu") {
        $body = @{ msg_type = "text"; content = @{ text = $Message } }
    } else {
        $body = $record
    }
    try {
        Invoke-RestMethod -Uri $webhook -Method Post -ContentType "application/json; charset=utf-8" -Body ($body | ConvertTo-Json -Depth 8) | Out-Null
    } catch {
        Write-WatchdogLog @{
            time = (Get-Date).ToUniversalTime().ToString("o")
            event = "WEBHOOK_FAILED"
            severity = "ERROR"
            message = $_.Exception.Message
        }
    }
}

$previousFingerprint = ""
$initialized = $false
if (Test-Path -LiteralPath $statePath) {
    try {
        $previousFingerprint = [string](Get-Content -LiteralPath $statePath -Raw -Encoding UTF8 | ConvertFrom-Json).fingerprint
        $initialized = $true
    } catch { }
}

try {
while ($true) {
    $errors = @()
    $snapshot = $null
    $health = $null
    try { $snapshot = Invoke-AutoTradeJson @("snapshot") } catch { $errors += $_.Exception.Message }
    try { $health = Invoke-AutoTradeJson @("health") } catch { $errors += $_.Exception.Message }
    $lockStatus = Get-DaemonLockStatus
    $issues = @(Get-Issues -Snapshot $snapshot -Health $health -LockStatus $lockStatus -CommandErrors $errors)
    $fingerprint = ($issues | ForEach-Object { "$($_.Severity):$($_.Code):$($_.Message)" }) -join "|"
    $now = (Get-Date).ToUniversalTime().ToString("o")

    if ($fingerprint -and $fingerprint -ne $previousFingerprint) {
        $highest = if ($issues.Severity -contains "CRITICAL") { "CRITICAL" } elseif ($issues.Severity -contains "ERROR") { "ERROR" } else { "WARNING" }
        Send-Alert -Event "WATCHDOG_ALERT" -Severity $highest -Message (($issues | ForEach-Object { "[$($_.Severity)] $($_.Message)" }) -join " | ") -Issues $issues
    } elseif (-not $fingerprint -and $previousFingerprint) {
        Send-Alert -Event "WATCHDOG_RECOVERED" -Severity "INFO" -Message "AutoTrade watchdog checks recovered" -Issues @()
    } elseif (-not $fingerprint -and -not $previousFingerprint -and -not $initialized) {
        Send-Alert -Event "WATCHDOG_STARTED" -Severity "INFO" -Message "AutoTrade watchdog is healthy" -Issues @()
    }

    $previousFingerprint = $fingerprint
    $initialized = $true
    (@{ fingerprint = $fingerprint; checked_at = $now } | ConvertTo-Json) | Set-Content -LiteralPath $statePath -Encoding UTF8
    if ($Once) { break }
    Start-Sleep -Seconds ([Math]::Max(10, $IntervalSeconds))
}
} finally {
    if (Test-Path -LiteralPath $watchdogLockPath) {
        try {
            $ownedLock = Get-Content -LiteralPath $watchdogLockPath -Raw -Encoding UTF8 | ConvertFrom-Json
            if ([int]$ownedLock.pid -eq $PID) {
                Remove-Item -LiteralPath $watchdogLockPath -Force
            }
        } catch { }
    }
}
