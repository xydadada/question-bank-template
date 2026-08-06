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

# Tear down the public edge first. A local proxy failure must never leave the
# Cloudflare route active merely because teardown stopped at the first error.
foreach ($Name in "cloudflared", "proxy") {
    $PidFile = Join-Path $Base "$Name.pid"
    if (-not (Test-Path $PidFile)) { continue }
    try { $SavedPid = [int](Get-Content -Raw -LiteralPath $PidFile) } catch { $SavedPid = 0 }
    $Row = if ($SavedPid) { Get-CimInstance Win32_Process -Filter "ProcessId=$SavedPid" -ErrorAction SilentlyContinue } else { $null }
    $Spec = $Expected[$Name]
    $MatchesExpected = $Row -and $Row.ExecutablePath -and (Test-Path $Spec.Path) -and
        [IO.Path]::GetFullPath($Row.ExecutablePath).Equals([IO.Path]::GetFullPath($Spec.Path), [StringComparison]::OrdinalIgnoreCase) -and
        $Row.CommandLine -match $Spec.Pattern
    if ($MatchesExpected) {
        try {
            Stop-Process -Id $SavedPid -ErrorAction Stop
            $Deadline = [DateTime]::UtcNow.AddSeconds(15)
            while ((Get-Process -Id $SavedPid -ErrorAction SilentlyContinue) -and [DateTime]::UtcNow -lt $Deadline) {
                Start-Sleep -Milliseconds 250
            }
            if (Get-Process -Id $SavedPid -ErrorAction SilentlyContinue) { throw "$Name did not exit within 15 seconds." }
            Write-Host "$Name stopped (PID $SavedPid)."
            Remove-Item -LiteralPath $PidFile -Force -ErrorAction SilentlyContinue
        } catch {
            $Failures.Add("$Name teardown failed: $($_.Exception.Message)")
        }
    } else {
        if ($Row) { Write-Warning "Ignored stale $Name PID file; PID $SavedPid belongs to another process." }
        Remove-Item -LiteralPath $PidFile -Force -ErrorAction SilentlyContinue
    }
    if ($Name -eq "proxy" -and -not (Test-Path $PidFile)) {
        Remove-Item -LiteralPath (Join-Path $Base "data\proxy-config.sha256") -Force -ErrorAction SilentlyContinue
    }
    if ($Name -eq "cloudflared" -and -not (Test-Path $PidFile)) {
        Remove-Item -LiteralPath (Join-Path $Base "cloudflare\runtime-config.sha256") -Force -ErrorAction SilentlyContinue
    }
}

if ($Failures.Count) {
    $Failures | ForEach-Object { Write-Warning $_ }
    throw "MCP teardown completed with $($Failures.Count) failure(s). Public tunnel teardown was attempted first."
}
Write-Host "MCP proxy and tunnel stop check complete."
