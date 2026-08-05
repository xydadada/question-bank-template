$Base = Split-Path -Parent $MyInvocation.MyCommand.Path
foreach ($Name in "proxy", "cloudflared") {
    $PidFile = Join-Path $Base "$Name.pid"
    $State = "stopped"
    if (Test-Path $PidFile) {
        $ProcessId = [int](Get-Content -Raw $PidFile)
        if (Get-Process -Id $ProcessId -ErrorAction SilentlyContinue) { $State = "running (PID $ProcessId)" }
    }
    Write-Host "$Name`: $State"
}
try {
    $Health = Invoke-WebRequest -UseBasicParsing -Uri "http://127.0.0.1:18081/healthz" -TimeoutSec 5
    Write-Host "local health: HTTP $($Health.StatusCode)"
} catch {
    Write-Host "local health: unavailable"
}
