[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)][string]$Hostname,
    [string]$TunnelName = "question-bank-mcp",
    [switch]$CreateDnsRoute,
    [switch]$ReuseExistingTunnel
)

$ErrorActionPreference = "Stop"
$Base = Split-Path -Parent $MyInvocation.MyCommand.Path
$Root = Split-Path -Parent $Base
$Cloudflared = Join-Path $Root "bin\cloudflared.exe"
if (-not (Test-Path $Cloudflared)) { throw "Run scripts/bootstrap.ps1 -InstallMcpTools first." }
if ($Hostname -eq "mcp.example.com" -or $Hostname -notmatch '^[A-Za-z0-9.-]+$') {
    throw "Pass a real hostname in a Cloudflare-managed zone."
}

function Protect-CloudflareCredentialPath([string]$Path, [switch]$Directory) {
    if (-not (Test-Path -LiteralPath $Path)) {
        throw "Cloudflare credential path not found: $Path"
    }
    $Sid = [Security.Principal.WindowsIdentity]::GetCurrent().User.Value
    & "$env:WINDIR\System32\icacls.exe" $Path /inheritance:r | Out-Null
    if ($LASTEXITCODE -ne 0) {
        throw "Could not disable inherited Cloudflare credential ACLs: $Path"
    }
    $ExpectedSids = @($Sid, "S-1-5-18")
    foreach ($Access in @((Get-Acl -LiteralPath $Path).Access)) {
        try {
            $AccessSid = $Access.IdentityReference.Translate(
                [Security.Principal.SecurityIdentifier]
            ).Value
            $IdentityArgument = "*$AccessSid"
        } catch {
            $AccessSid = ""
            $IdentityArgument = $Access.IdentityReference.Value
        }
        if ($AccessSid -in $ExpectedSids) { continue }
        $RemoveOption = if (
            $Access.AccessControlType -eq [Security.AccessControl.AccessControlType]::Deny
        ) { "/remove:d" } else { "/remove:g" }
        & "$env:WINDIR\System32\icacls.exe" $Path $RemoveOption $IdentityArgument | Out-Null
        if ($LASTEXITCODE -ne 0) {
            throw "Could not remove an extra Cloudflare credential ACL: $Path"
        }
    }
    $UserGrant = if ($Directory) { "*${Sid}:(OI)(CI)F" } else { "*${Sid}:F" }
    $SystemGrant = if ($Directory) { "*S-1-5-18:(OI)(CI)F" } else { "*S-1-5-18:F" }
    & "$env:WINDIR\System32\icacls.exe" $Path /grant:r `
        $UserGrant $SystemGrant | Out-Null
    if ($LASTEXITCODE -ne 0) {
        throw "Could not restrict Cloudflare credential ACLs: $Path"
    }
    $Unexpected = @((Get-Acl -LiteralPath $Path).Access | Where-Object {
        try {
            $_.IdentityReference.Translate(
                [Security.Principal.SecurityIdentifier]
            ).Value -notin $ExpectedSids
        } catch { $true }
    })
    if ($Unexpected.Count) {
        throw "Cloudflare credential ACL verification failed: $Path"
    }
}

$CredentialDir = Join-Path $env:USERPROFILE ".cloudflared"
$Certificate = Join-Path $CredentialDir "cert.pem"
if (-not (Test-Path $Certificate)) {
    & $Cloudflared tunnel login
    if ($LASTEXITCODE -ne 0) { throw "Cloudflare login failed." }
}
Protect-CloudflareCredentialPath $CredentialDir -Directory
Protect-CloudflareCredentialPath $Certificate

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
    if (-not $ReuseExistingTunnel) {
        throw "Cloudflare Tunnel '$TunnelName' already exists. Confirm its ownership, then rerun with -ReuseExistingTunnel."
    }
    $TunnelId = [string]$Tunnel.id
    Write-Host "Reusing explicitly approved Cloudflare Tunnel '$TunnelName' ($TunnelId)."
}

$Credentials = Join-Path $CredentialDir "$TunnelId.json"
if (-not (Test-Path $Credentials)) { throw "Tunnel credentials not found: $Credentials" }
Protect-CloudflareCredentialPath $Credentials
if ($CreateDnsRoute) {
    & $Cloudflared tunnel route dns $TunnelId $Hostname
    if ($LASTEXITCODE -ne 0) { throw "Cloudflare DNS route creation failed." }
} else {
    Write-Warning "DNS was not changed. Re-run with -CreateDnsRoute after reviewing the hostname."
}

$ConfigDir = Join-Path $Base "cloudflare"
New-Item -ItemType Directory -Force -Path $ConfigDir | Out-Null
$Sid = [Security.Principal.WindowsIdentity]::GetCurrent().User.Value
& "$env:WINDIR\System32\icacls.exe" $ConfigDir /inheritance:r /grant:r "*${Sid}:(OI)(CI)F" "*S-1-5-18:(OI)(CI)F" | Out-Null
if ($LASTEXITCODE -ne 0) { throw "Could not restrict Cloudflare configuration ACLs." }
$Config = @"
tunnel: $TunnelId
credentials-file: $($Credentials.Replace('\', '/'))
ingress:
  - hostname: $Hostname
    service: http://127.0.0.1:18081
  - service: http_status:404
"@
[IO.File]::WriteAllText((Join-Path $ConfigDir "config.yml"), $Config, [Text.UTF8Encoding]::new($false))
Write-Host "Cloudflare Tunnel config created locally. Start the complete MCP path with:"
Write-Host "powershell -File .\mcp-public\start-all.ps1 -ExternalUrl https://$Hostname"
