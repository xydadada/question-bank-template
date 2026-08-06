$ErrorActionPreference = "Stop"
foreach ($Probe in @(
    @{ Name = "WeKnora API"; Uri = "http://127.0.0.1:8080/health" },
    @{ Name = "Ollama"; Uri = "http://127.0.0.1:11434/api/tags" },
    @{ Name = "MCP proxy"; Uri = "http://127.0.0.1:18081/healthz" },
    @{ Name = "OAuth discovery"; Uri = "http://127.0.0.1:18081/.well-known/oauth-authorization-server" }
)) {
    $Response = Invoke-WebRequest -UseBasicParsing -Uri $Probe.Uri -TimeoutSec 10
    if ($Response.StatusCode -ne 200) { throw "$($Probe.Name) returned HTTP $($Response.StatusCode)." }
}
try {
    $Body = '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-03-26","capabilities":{},"clientInfo":{"name":"template-test","version":"1.0"}}}'
    Invoke-WebRequest -UseBasicParsing -Method Post -Uri "http://127.0.0.1:18081/mcp" `
        -ContentType "application/json" -Body $Body -TimeoutSec 10 | Out-Null
    throw "Unauthenticated /mcp access was unexpectedly accepted."
} catch {
    $Status = if ($_.Exception.Response) { [int]$_.Exception.Response.StatusCode } else { 0 }
    if ($Status -notin 401, 403) { throw }
}
Write-Host "Core services, OAuth discovery, and unauthenticated-access rejection passed."
