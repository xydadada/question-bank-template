[CmdletBinding()]
param([Parameter(Mandatory = $true)][string]$ExternalUrl)

$ErrorActionPreference = "Stop"
$Base = Split-Path -Parent $MyInvocation.MyCommand.Path
$Root = Split-Path -Parent $Base
$Cloudflared = Join-Path $Root "bin\cloudflared.exe"
$Config = Join-Path $Base "cloudflare\config.yml"
$LogDir = Join-Path $Base "logs"
$PidFile = Join-Path $Base "cloudflared.pid"
$FingerprintFile = Join-Path $Base "cloudflare\runtime-config.sha256"
foreach ($Required in $Cloudflared, $Config) {
    if (-not (Test-Path $Required -PathType Leaf)) { throw "Missing required file: $Required" }
}
$HostnameMatch = Select-String -LiteralPath $Config -Pattern '^\s*-\s*hostname:\s*(\S+)\s*$' | Select-Object -First 1
if (-not $HostnameMatch) { throw "Cloudflare config has no hostname ingress rule." }
$Hostname = $HostnameMatch.Matches[0].Groups[1].Value
$ParsedExternalUrl = $null
if (-not [Uri]::TryCreate($ExternalUrl, [UriKind]::Absolute, [ref]$ParsedExternalUrl) -or
    $ParsedExternalUrl.Scheme -ne "https" -or -not $ParsedExternalUrl.Host -or
    $ParsedExternalUrl.UserInfo -or $ParsedExternalUrl.Query -or $ParsedExternalUrl.Fragment -or
    $ParsedExternalUrl.AbsolutePath -ne "/") {
    throw "ExternalUrl must be an HTTPS origin without a path, query, credentials or fragment."
}
if (-not $ParsedExternalUrl.Host.Equals($Hostname, [StringComparison]::OrdinalIgnoreCase)) {
    throw "ExternalUrl host '$($ParsedExternalUrl.Host)' does not match Cloudflare ingress hostname '$Hostname'."
}
$PublicBase = $ParsedExternalUrl.GetLeftPart([UriPartial]::Authority).TrimEnd('/')
$ExpectedFingerprint = (Get-FileHash -Algorithm SHA256 -LiteralPath $Config).Hash.ToLowerInvariant()
if (Test-Path $PidFile) {
    try { $ExistingPid = [int](Get-Content -Raw -LiteralPath $PidFile) } catch { $ExistingPid = 0 }
    $Existing = if ($ExistingPid) { Get-CimInstance Win32_Process -Filter "ProcessId=$ExistingPid" -ErrorAction SilentlyContinue } else { $null }
    $ExpectedPath = [IO.Path]::GetFullPath($Cloudflared)
    $MatchesCloudflared = $Existing -and $Existing.ExecutablePath -and
        [IO.Path]::GetFullPath($Existing.ExecutablePath).Equals($ExpectedPath, [StringComparison]::OrdinalIgnoreCase) -and
        $Existing.CommandLine -match '(?i)tunnel.*run' -and
        $Existing.CommandLine -match [regex]::Escape($Config)
    if ($MatchesCloudflared) {
        $StoredFingerprint = if (Test-Path $FingerprintFile) { (Get-Content -Raw -LiteralPath $FingerprintFile).Trim() } else { "" }
        if ($StoredFingerprint -ne $ExpectedFingerprint) {
            throw "cloudflared is already running with a changed config. Run mcp-public/stop.ps1, then start again."
        }
        $Healthy = $false
        try { $Healthy = (Invoke-WebRequest -UseBasicParsing -Uri "$PublicBase/healthz" -TimeoutSec 5).StatusCode -eq 200 } catch { }
        if ($Healthy) {
            Write-Host "cloudflared is already publicly healthy (PID $ExistingPid)."
            return [pscustomobject]@{ Started = $false; ProcessId = $ExistingPid }
        }
        Stop-Process -Id $ExistingPid
        $StopDeadline = [DateTime]::UtcNow.AddSeconds(15)
        while ((Get-Process -Id $ExistingPid -ErrorAction SilentlyContinue) -and [DateTime]::UtcNow -lt $StopDeadline) { Start-Sleep -Milliseconds 250 }
        if (Get-Process -Id $ExistingPid -ErrorAction SilentlyContinue) { throw "Unhealthy cloudflared process could not be stopped safely." }
    }
    Remove-Item -LiteralPath $PidFile -Force -ErrorAction SilentlyContinue
    Remove-Item -LiteralPath $FingerprintFile -Force -ErrorAction SilentlyContinue
}
New-Item -ItemType Directory -Force -Path $LogDir | Out-Null
$Sid = [Security.Principal.WindowsIdentity]::GetCurrent().User.Value
& "$env:WINDIR\System32\icacls.exe" $LogDir /inheritance:r /grant:r "*${Sid}:(OI)(CI)F" "*S-1-5-18:(OI)(CI)F" | Out-Null
if ($LASTEXITCODE -ne 0) { throw "Could not restrict MCP log ACLs." }

$QuotedConfig = '"' + $Config.Replace('"', '\"') + '"'
$Process = Start-Process -FilePath $Cloudflared -ArgumentList @("tunnel", "--config", $QuotedConfig, "run") `
    -WorkingDirectory $Root -WindowStyle Hidden -PassThru `
    -RedirectStandardOutput (Join-Path $LogDir "cloudflared.stdout.log") `
    -RedirectStandardError (Join-Path $LogDir "cloudflared.stderr.log")
[IO.File]::WriteAllText($PidFile, [string]$Process.Id, [Text.UTF8Encoding]::new($false))
Start-Sleep -Seconds 3
if ($Process.HasExited) {
    Remove-Item -LiteralPath $PidFile -Force -ErrorAction SilentlyContinue
    throw "cloudflared exited. See mcp-public/logs/cloudflared.stderr.log."
}
$Deadline = [DateTime]::UtcNow.AddSeconds(90)
do {
    if ($Process.HasExited) { break }
    try {
        $PublicHealth = Invoke-WebRequest -UseBasicParsing -Uri "$PublicBase/healthz" -TimeoutSec 5
        if ($PublicHealth.StatusCode -eq 200) {
            [IO.File]::WriteAllText($FingerprintFile, $ExpectedFingerprint, [Text.UTF8Encoding]::new($false))
            Write-Host "Cloudflare Tunnel is publicly healthy (PID $($Process.Id))."
            return [pscustomobject]@{ Started = $true; ProcessId = $Process.Id }
        }
    } catch { }
    Start-Sleep -Seconds 2
} while ([DateTime]::UtcNow -lt $Deadline)

if (-not $Process.HasExited) {
    Stop-Process -Id $Process.Id -ErrorAction SilentlyContinue
    $StopDeadline = [DateTime]::UtcNow.AddSeconds(15)
    while ((Get-Process -Id $Process.Id -ErrorAction SilentlyContinue) -and [DateTime]::UtcNow -lt $StopDeadline) { Start-Sleep -Milliseconds 250 }
}
if (Get-Process -Id $Process.Id -ErrorAction SilentlyContinue) {
    throw "cloudflared stayed alive but the public health check failed; PID file was retained."
}
Remove-Item -LiteralPath $PidFile -Force -ErrorAction SilentlyContinue
Remove-Item -LiteralPath $FingerprintFile -Force -ErrorAction SilentlyContinue
throw "Cloudflare Tunnel did not become publicly healthy within 90 seconds. Check DNS and mcp-public/logs/cloudflared.stderr.log."
