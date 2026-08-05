$ErrorActionPreference = "Stop"
$Health = Invoke-WebRequest -UseBasicParsing -Uri "http://127.0.0.1:18081/healthz" -TimeoutSec 10
if ($Health.StatusCode -ne 200) { throw "Unexpected /healthz status: $($Health.StatusCode)" }
try {
    Invoke-WebRequest -UseBasicParsing -Uri "http://127.0.0.1:18081/mcp" -TimeoutSec 10 | Out-Null
    throw "Unauthenticated /mcp access was unexpectedly accepted."
} catch {
    $Status = [int]$_.Exception.Response.StatusCode
    if ($Status -notin 401, 403, 405) { throw }
}
Write-Host "Local proxy health and unauthenticated-access rejection passed."
