[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)][string]$Hostname,
    [string]$TunnelName = "question-bank-mcp",
    [switch]$CreateDnsRoute
)

$ErrorActionPreference = "Stop"
$Base = Split-Path -Parent $MyInvocation.MyCommand.Path
$Root = Split-Path -Parent $Base
$Cloudflared = Join-Path $Root "bin\cloudflared.exe"
if (-not (Test-Path $Cloudflared)) { throw "Run scripts/bootstrap.ps1 -InstallMcpTools first." }
if ($Hostname -eq "mcp.example.com" -or $Hostname -notmatch '^[A-Za-z0-9.-]+$') {
    throw "Pass a real hostname in a Cloudflare-managed zone."
}

$Certificate = Join-Path $env:USERPROFILE ".cloudflared\cert.pem"
if (-not (Test-Path $Certificate)) {
    & $Cloudflared tunnel login
    if ($LASTEXITCODE -ne 0) { throw "Cloudflare login failed." }
}

$Existing = (& $Cloudflared tunnel list --output json 2>$null) -join "`n" | ConvertFrom-Json
$Tunnel = @($Existing) | Where-Object { $_.name -eq $TunnelName } | Select-Object -First 1
if (-not $Tunnel) {
    $CreatedText = (& $Cloudflared tunnel create $TunnelName 2>&1) -join "`n"
    if ($LASTEXITCODE -ne 0) { throw $CreatedText }
    if ($CreatedText -notmatch '[0-9a-fA-F]{8}-[0-9a-fA-F-]{27,}') {
        throw "Tunnel was created but its ID could not be parsed. Inspect: cloudflared tunnel list"
    }
    $TunnelId = $Matches[0]
} else {
    $TunnelId = [string]$Tunnel.id
}

$Credentials = Join-Path $env:USERPROFILE ".cloudflared\$TunnelId.json"
if (-not (Test-Path $Credentials)) { throw "Tunnel credentials not found: $Credentials" }
if ($CreateDnsRoute) {
    & $Cloudflared tunnel route dns $TunnelId $Hostname
    if ($LASTEXITCODE -ne 0) { throw "Cloudflare DNS route creation failed." }
} else {
    Write-Warning "DNS was not changed. Re-run with -CreateDnsRoute after reviewing the hostname."
}

$ConfigDir = Join-Path $Base "cloudflare"
New-Item -ItemType Directory -Force -Path $ConfigDir | Out-Null
$Config = @"
tunnel: $TunnelId
credentials-file: $($Credentials.Replace('\', '/'))
ingress:
  - hostname: $Hostname
    service: http://127.0.0.1:18081
  - service: http_status:404
"@
[IO.File]::WriteAllText((Join-Path $ConfigDir "config.yml"), $Config, [Text.UTF8Encoding]::new($false))
Write-Host "Cloudflare Tunnel config created locally. Start it with mcp-public/start-cloudflare.ps1."
