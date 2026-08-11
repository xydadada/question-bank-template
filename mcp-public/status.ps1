[CmdletBinding()]
param([string]$ExternalUrl = $env:MCP_EXTERNAL_URL)

$Base = Split-Path -Parent $MyInvocation.MyCommand.Path
$Root = Split-Path -Parent $Base
$Expected = @{
    proxy = @{ Path = (Join-Path $Root "bin\mcp-auth-proxy.exe"); Pattern = '127\.0\.0\.1:18081' }
    cloudflared = @{
        Path = (Join-Path $Root "bin\cloudflared.exe")
        Pattern = ('(?i)tunnel.*' + [regex]::Escape((Join-Path $Base "cloudflare\config.yml")) + '.*run')
    }
}

function Show-HttpStatus([string]$Name, [string]$Uri, [int[]]$Accepted = @(200)) {
    try {
        $Response = Invoke-WebRequest -UseBasicParsing -Uri $Uri -TimeoutSec 5
        $Code = [int]$Response.StatusCode
    } catch {
        $Code = if ($_.Exception.Response) { [int]$_.Exception.Response.StatusCode } else { 0 }
    }
    $Display = if ($Code) { "HTTP $Code" } else { "unavailable" }
    $Suffix = if ($Code -in $Accepted) { "OK" } else { "CHECK" }
    Write-Host "$Name`: $Display [$Suffix]"
}

function Show-UnauthenticatedMcpRejection([string]$Uri) {
    $Body = '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-03-26","capabilities":{},"clientInfo":{"name":"template-status","version":"1.0"}}}'
    try {
        $Response = Invoke-WebRequest -UseBasicParsing -Method Post -Uri $Uri `
            -ContentType "application/json" -Body $Body -TimeoutSec 5
        $Code = [int]$Response.StatusCode
    } catch {
        $Code = if ($_.Exception.Response) { [int]$_.Exception.Response.StatusCode } else { 0 }
    }
    $Display = if ($Code) { "HTTP $Code" } else { "unavailable" }
    $Suffix = if ($Code -in 401, 403) { "OK" } else { "CHECK" }
    Write-Host "local unauthenticated MCP initialize: $Display [$Suffix]"
}

foreach ($Name in "proxy", "cloudflared") {
    $PidFile = Join-Path $Base "$Name.pid"
    $SavedPid = 0
    if (Test-Path $PidFile) {
        try { $SavedPid = [int](Get-Content -Raw -LiteralPath $PidFile) } catch { $SavedPid = 0 }
    }
    $Spec = $Expected[$Name]
    $Managed = @()
    if (Test-Path $Spec.Path) {
        $ExpectedPath = [IO.Path]::GetFullPath($Spec.Path)
        $Managed = @(Get-CimInstance Win32_Process -ErrorAction SilentlyContinue | Where-Object {
            $_.ExecutablePath -and
            [IO.Path]::GetFullPath($_.ExecutablePath).Equals($ExpectedPath, [StringComparison]::OrdinalIgnoreCase) -and
            $_.CommandLine -match $Spec.Pattern
        })
    }
    $State = if ($Managed.Count -gt 1) {
        "ERROR: duplicate managed processes (PIDs $(@($Managed.ProcessId) -join ', '))"
    } elseif ($Managed.Count -eq 1) {
        $Suffix = if ($SavedPid -eq $Managed[0].ProcessId) { "verified PID file" } else { "PID file missing or stale" }
        "running (PID $($Managed[0].ProcessId), $Suffix)"
    } elseif (Test-Path $PidFile) {
        "stopped (stale PID file)"
    } else {
        "stopped"
    }
    Write-Host "$Name`: $State"
}

Show-HttpStatus "WeKnora API" "http://127.0.0.1:8080/health"
Show-HttpStatus "Ollama" "http://127.0.0.1:11434/api/tags"
Show-HttpStatus "local MCP health" "http://127.0.0.1:18081/healthz"
Show-HttpStatus "local OAuth discovery" "http://127.0.0.1:18081/.well-known/oauth-authorization-server"
Show-UnauthenticatedMcpRejection "http://127.0.0.1:18081/mcp"

if (-not $ExternalUrl) {
    $Config = Join-Path $Base "cloudflare\config.yml"
    if (Test-Path $Config) {
        $Hostname = Select-String -LiteralPath $Config -Pattern '^\s*-\s*hostname:\s*(\S+)\s*$' | Select-Object -First 1
        if ($Hostname) { $ExternalUrl = "https://$($Hostname.Matches[0].Groups[1].Value)" }
    }
}
if ($ExternalUrl) {
    $PublicBase = $ExternalUrl.TrimEnd('/')
    Show-HttpStatus "public MCP health" "$PublicBase/healthz"
    Show-HttpStatus "public OAuth discovery" "$PublicBase/.well-known/oauth-authorization-server"
}
