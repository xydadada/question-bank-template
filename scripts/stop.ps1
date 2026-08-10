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
        Stop-Process -Id $SavedPid
        $Deadline = [DateTime]::UtcNow.AddSeconds(15)
        while ((Get-Process -Id $SavedPid -ErrorAction SilentlyContinue) -and [DateTime]::UtcNow -lt $Deadline) {
            Start-Sleep -Milliseconds 250
        }
        if (Get-Process -Id $SavedPid -ErrorAction SilentlyContinue) { throw "$Name did not stop." }
        Write-Host "$Name stopped (PID $SavedPid)."
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
