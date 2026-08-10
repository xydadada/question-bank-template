[CmdletBinding()]
param([string]$WslDistro, [switch]$StopWeKnora)

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
$Runtime = Join-Path $Root ".runtime"
$WslExe = Join-Path $env:WINDIR "System32\wsl.exe"
$DistroFile = Join-Path $Runtime "wsl-distro.txt"
if (-not $WslDistro -and (Test-Path $DistroFile)) {
    $WslDistro = (Get-Content -Raw -LiteralPath $DistroFile).Trim()
}
if (-not $WslDistro) { $WslDistro = "Ubuntu" }

function Stop-ProcessTree([int]$RootProcessId, [int]$TimeoutSeconds = 15) {
    $Known = [Collections.Generic.HashSet[int]]::new()
    $Known.Add($RootProcessId) | Out-Null
    $Discover = {
        param($Rows, $KnownIds)
        $Changed = $true
        while ($Changed) {
            $Changed = $false
            foreach ($Child in $Rows) {
                if ($KnownIds.Contains([int]$Child.ParentProcessId) -and
                    -not $KnownIds.Contains([int]$Child.ProcessId)) {
                    $KnownIds.Add([int]$Child.ProcessId) | Out-Null
                    $Changed = $true
                }
            }
        }
    }
    $Rows = @(Get-CimInstance Win32_Process -ErrorAction Stop)
    & $Discover $Rows $Known
    # Stop the supervisor first so it cannot replace a worker while the
    # descendants are being collected and terminated.
    Stop-Process -Id $RootProcessId -Force -ErrorAction SilentlyContinue
    $Deadline = [DateTime]::UtcNow.AddSeconds($TimeoutSeconds)
    do {
        $Rows = @(Get-CimInstance Win32_Process -ErrorAction Stop)
        & $Discover $Rows $Known
        $Remaining = @($Rows | Where-Object {
            $Known.Contains([int]$_.ProcessId)
        })
        foreach ($ProcessRow in $Remaining) {
            Stop-Process -Id $ProcessRow.ProcessId -Force -ErrorAction SilentlyContinue
        }
        Start-Sleep -Milliseconds 250
        $Rows = @(Get-CimInstance Win32_Process -ErrorAction Stop)
        & $Discover $Rows $Known
        $Remaining = @($Rows | Where-Object {
            $Known.Contains([int]$_.ProcessId)
        })
        if (-not $Remaining.Count) { return }
    } while ([DateTime]::UtcNow -lt $Deadline)
    $RemainingIds = @($Remaining | ForEach-Object { $_.ProcessId })
    throw "Process tree still has running PIDs: $($RemainingIds -join ', ')"
}

function Stop-ManagedProcess(
    [string]$PidFile,
    [string]$ExpectedExecutable,
    [string]$CommandPattern,
    [string]$Name
) {
    if (-not (Test-Path $PidFile)) { return }
    try { $SavedPid = [int](Get-Content -Raw -LiteralPath $PidFile) } catch {
        Remove-Item -LiteralPath $PidFile -Force -ErrorAction SilentlyContinue
        return
    }
    $Row = Get-CimInstance Win32_Process -Filter "ProcessId=$SavedPid" -ErrorAction SilentlyContinue
    $Expected = [IO.Path]::GetFullPath($ExpectedExecutable)
    $MatchesExpected = $Row -and $Row.ExecutablePath -and
        [IO.Path]::GetFullPath($Row.ExecutablePath).Equals($Expected, [StringComparison]::OrdinalIgnoreCase) -and
        (!$CommandPattern -or $Row.CommandLine -match $CommandPattern)
    if ($MatchesExpected) {
        Stop-ProcessTree $SavedPid
        Write-Host "$Name process tree stopped (root PID $SavedPid)."
    } elseif ($Row) {
        Write-Warning "Ignored stale $Name PID file; PID $SavedPid belongs to another process."
    }
    Remove-Item -LiteralPath $PidFile -Force -ErrorAction SilentlyContinue
}

$Failures = [Collections.Generic.List[string]]::new()
try { & (Join-Path $Root "mcp-public\stop.ps1") } catch { $Failures.Add($_.Exception.Message) }

$Python = Join-Path $Root ".venv\Scripts\python.exe"
$IngestScript = Join-Path $Root "ingest.py"
$IngestPattern = '(?i)' + [regex]::Escape($IngestScript) + '.*--supervise'
try {
    Stop-ManagedProcess (Join-Path $Runtime "ingest.pid") $Python $IngestPattern "ingestion supervisor"
} catch { $Failures.Add($_.Exception.Message) }

if ($StopWeKnora) {
    $WeKnora = Join-Path $Runtime "WeKnora"
    if (Test-Path $WeKnora) {
        & $WslExe -d $WslDistro --cd $WeKnora -- docker compose --profile "*" stop
        if ($LASTEXITCODE -ne 0) { $Failures.Add("WeKnora Docker stop failed.") }
    }
    $DistroPattern = [regex]::Escape($WslDistro)
    $KeepAlivePattern = '(?i)-d\s+"?' + $DistroPattern + '"?.*sleep\s+infinity'
    try {
        Stop-ManagedProcess (Join-Path $Runtime "wsl-keepalive.pid") $WslExe $KeepAlivePattern "WSL keepalive"
    } catch { $Failures.Add($_.Exception.Message) }
    if (-not $Failures.Count) { Remove-Item -LiteralPath $DistroFile -Force -ErrorAction SilentlyContinue }
}

if ($Failures.Count) {
    $Failures | ForEach-Object { Write-Warning $_ }
    throw "Stop completed with $($Failures.Count) failure(s); every teardown step was attempted."
}
Write-Host "Local workers stopped. WeKnoraStopped=$StopWeKnora"
