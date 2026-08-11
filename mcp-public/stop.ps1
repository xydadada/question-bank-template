$ErrorActionPreference = "Stop"
$Base = Split-Path -Parent $MyInvocation.MyCommand.Path
$Root = Split-Path -Parent $Base
$Failures = [Collections.Generic.List[string]]::new()
$Expected = @{
    proxy = @{ Path = (Join-Path $Root "bin\mcp-auth-proxy.exe"); Pattern = '127\.0\.0\.1:18081' }
    cloudflared = @{
        Path = (Join-Path $Root "bin\cloudflared.exe")
        Pattern = ('(?i)tunnel.*' + [regex]::Escape((Join-Path $Base "cloudflare\config.yml")) + '.*run')
    }
}

function Stop-ProcessTree([int]$RootProcessId, [string]$Name) {
    $Rows = @(Get-CimInstance Win32_Process -ErrorAction SilentlyContinue)
    $Pending = [Collections.Generic.Stack[int]]::new()
    $Order = [Collections.Generic.List[int]]::new()
    $Pending.Push($RootProcessId)
    while ($Pending.Count) {
        $Current = $Pending.Pop()
        if ($Order.Contains($Current)) { continue }
        $Order.Add($Current)
        foreach ($Child in $Rows | Where-Object { $_.ParentProcessId -eq $Current }) {
            $Pending.Push([int]$Child.ProcessId)
        }
    }
    # Stop the supervisor first so it cannot spawn a replacement child while
    # the already captured descendants are being terminated.
    Stop-Process -Id $RootProcessId -Force -ErrorAction SilentlyContinue
    for ($Index = $Order.Count - 1; $Index -ge 0; $Index--) {
        Stop-Process -Id $Order[$Index] -ErrorAction SilentlyContinue
    }
    $Deadline = [DateTime]::UtcNow.AddSeconds(15)
    do {
        $RemainingIds = @($Order | Where-Object {
            Get-Process -Id $_ -ErrorAction SilentlyContinue
        })
        if (-not $RemainingIds.Count) { return }
        Start-Sleep -Milliseconds 250
    } while ([DateTime]::UtcNow -lt $Deadline)
    throw "$Name process tree still has running PIDs: $($RemainingIds -join ', ')"
}

$MutexHasher = [Security.Cryptography.SHA256]::Create()
try {
    $MutexDigest = ([BitConverter]::ToString(
        $MutexHasher.ComputeHash([Text.Encoding]::UTF8.GetBytes($Root.ToLowerInvariant()))
    )).Replace('-', '').Substring(0, 24)
} finally { $MutexHasher.Dispose() }
$LifecycleMutex = [Threading.Mutex]::new($false, "Local\QuestionBank-$MutexDigest-McpLifecycle")
$LifecycleMutexOwned = $false
try { $LifecycleMutexOwned = $LifecycleMutex.WaitOne(0) } catch [Threading.AbandonedMutexException] { $LifecycleMutexOwned = $true }
if (-not $LifecycleMutexOwned) {
    $LifecycleMutex.Dispose()
    throw "Another MCP start or stop operation is already running for this template."
}

try {
# Tear down the public edge first. A local proxy failure must never leave the
# Cloudflare route active merely because teardown stopped at the first error.
foreach ($Name in "cloudflared", "proxy") {
    $PidFile = Join-Path $Base "$Name.pid"
    $Spec = $Expected[$Name]
    $SavedPid = 0
    if (Test-Path $PidFile) {
        try { $SavedPid = [int](Get-Content -Raw -LiteralPath $PidFile) } catch { $SavedPid = 0 }
    }
    $ExpectedPath = [IO.Path]::GetFullPath($Spec.Path)
    $Rows = @(Get-CimInstance Win32_Process -ErrorAction SilentlyContinue | Where-Object {
        $_.ExecutablePath -and
        [IO.Path]::GetFullPath($_.ExecutablePath).Equals($ExpectedPath, [StringComparison]::OrdinalIgnoreCase) -and
        $_.CommandLine -match $Spec.Pattern
    })
    if ($SavedPid -and -not ($Rows | Where-Object { $_.ProcessId -eq $SavedPid })) {
        $SavedRow = Get-CimInstance Win32_Process -Filter "ProcessId=$SavedPid" -ErrorAction SilentlyContinue
        if ($SavedRow) { Write-Warning "Ignored stale $Name PID file; PID $SavedPid belongs to another process." }
    }
    foreach ($Row in $Rows) {
        try {
            Stop-ProcessTree ([int]$Row.ProcessId) $Name
            Write-Host "$Name stopped (PID $($Row.ProcessId))."
        } catch {
            $Failures.Add("$Name teardown failed: $($_.Exception.Message)")
        }
    }
    $Remaining = @(Get-CimInstance Win32_Process -ErrorAction SilentlyContinue | Where-Object {
        $_.ExecutablePath -and
        [IO.Path]::GetFullPath($_.ExecutablePath).Equals($ExpectedPath, [StringComparison]::OrdinalIgnoreCase) -and
        $_.CommandLine -match $Spec.Pattern
    })
    if (-not $Remaining.Count) {
        Remove-Item -LiteralPath $PidFile -Force -ErrorAction SilentlyContinue
    }
    if ($Name -eq "proxy" -and -not $Remaining.Count) {
        Remove-Item -LiteralPath (Join-Path $Base "data\proxy-config.sha256") -Force -ErrorAction SilentlyContinue
    }
    if ($Name -eq "cloudflared" -and -not $Remaining.Count) {
        Remove-Item -LiteralPath (Join-Path $Base "cloudflare\runtime-config.sha256") -Force -ErrorAction SilentlyContinue
    }
}

if ($Failures.Count) {
    $Failures | ForEach-Object { Write-Warning $_ }
    throw "MCP teardown completed with $($Failures.Count) failure(s). Public tunnel teardown was attempted first."
}
Write-Host "MCP proxy and tunnel stop check complete."
} finally {
    if ($LifecycleMutexOwned) { $LifecycleMutex.ReleaseMutex() }
    $LifecycleMutex.Dispose()
}
