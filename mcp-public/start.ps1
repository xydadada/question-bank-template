[CmdletBinding()]
param(
    [string]$ExternalUrl = $env:MCP_EXTERNAL_URL,
    [string]$Profile = $(if ($env:WEKNORA_MCP_PROFILE) { $env:WEKNORA_MCP_PROFILE } else { "mcp-readonly" })
)

$ErrorActionPreference = "Stop"
$Base = Split-Path -Parent $MyInvocation.MyCommand.Path
$Root = Split-Path -Parent $Base
$Proxy = Join-Path $Root "bin\mcp-auth-proxy.exe"
$WeKnora = Join-Path $Root "bin\weknora.exe"
$HashFile = Join-Path $Base "secrets\password-hash.txt"
$DataDir = Join-Path $Base "data"
$LogDir = Join-Path $Base "logs"
$PidFile = Join-Path $Base "proxy.pid"
$FingerprintFile = Join-Path $DataDir "proxy-config.sha256"

if ($Profile -notmatch '^[A-Za-z0-9._-]+$') { throw "Profile names may only contain letters, numbers, dot, underscore and hyphen." }
if (-not $ExternalUrl) { throw "Pass -ExternalUrl https://your-mcp-hostname." }
$ParsedExternalUrl = $null
if (-not [Uri]::TryCreate($ExternalUrl, [UriKind]::Absolute, [ref]$ParsedExternalUrl) -or
    $ParsedExternalUrl.Scheme -ne "https" -or -not $ParsedExternalUrl.Host -or
    $ParsedExternalUrl.UserInfo -or $ParsedExternalUrl.Query -or $ParsedExternalUrl.Fragment -or
    $ParsedExternalUrl.AbsolutePath -ne "/") {
    throw "ExternalUrl must be an HTTPS origin without a path, query, credentials or fragment."
}
$ExternalUrl = $ParsedExternalUrl.GetLeftPart([UriPartial]::Authority).TrimEnd('/')
if ($ExternalUrl -eq "https://mcp.example.com") { throw "Replace the example MCP URL first." }
foreach ($Required in $Proxy, $WeKnora, $HashFile) {
    if (-not (Test-Path $Required -PathType Leaf)) { throw "Missing required file: $Required" }
}
try {
    $Backend = Invoke-WebRequest -UseBasicParsing -Uri "http://127.0.0.1:8080/health" -TimeoutSec 5
    if ($Backend.StatusCode -ne 200) { throw "unexpected status" }
} catch { throw "WeKnora API is not ready on 127.0.0.1:8080. Start the core stack first." }

$ProfileJson = (& $WeKnora profile list --format json 2>$null) -join "`n"
if ($LASTEXITCODE -ne 0 -or -not (@(($ProfileJson | ConvertFrom-Json).data) | Where-Object { $_.name -eq $Profile })) {
    throw "Missing MCP profile '$Profile'. Run mcp-public/configure-readonly-profile.ps1 first."
}

New-Item -ItemType Directory -Force -Path $DataDir, $LogDir, (Split-Path -Parent $HashFile) | Out-Null
$Sid = [Security.Principal.WindowsIdentity]::GetCurrent().User.Value
foreach ($Directory in $DataDir, (Split-Path -Parent $HashFile), $LogDir) {
    & "$env:WINDIR\System32\icacls.exe" $Directory /inheritance:r /grant:r "*${Sid}:(OI)(CI)F" "*S-1-5-18:(OI)(CI)F" | Out-Null
    if ($LASTEXITCODE -ne 0) { throw "Could not restrict ACLs on $Directory" }
}
$PasswordHash = (Get-Content -Raw -LiteralPath $HashFile).Trim()
$FingerprintBytes = [Text.Encoding]::UTF8.GetBytes("$ExternalUrl`n$Profile`n$PasswordHash")
$Hasher = [Security.Cryptography.SHA256]::Create()
try { $ExpectedFingerprint = ([BitConverter]::ToString($Hasher.ComputeHash($FingerprintBytes))).Replace('-', '').ToLowerInvariant() } finally {
    $Hasher.Dispose()
    [Array]::Clear($FingerprintBytes, 0, $FingerprintBytes.Length)
}

if (Test-Path $PidFile) {
    try { $ExistingPid = [int](Get-Content -Raw -LiteralPath $PidFile) } catch { $ExistingPid = 0 }
    $Existing = if ($ExistingPid) { Get-CimInstance Win32_Process -Filter "ProcessId=$ExistingPid" -ErrorAction SilentlyContinue } else { $null }
    $ExpectedPath = [IO.Path]::GetFullPath($Proxy)
    $MatchesProxy = $Existing -and $Existing.ExecutablePath -and
        [IO.Path]::GetFullPath($Existing.ExecutablePath).Equals($ExpectedPath, [StringComparison]::OrdinalIgnoreCase) -and
        $Existing.CommandLine -match '127\.0\.0\.1:18081'
    if ($MatchesProxy) {
        $StoredFingerprint = if (Test-Path $FingerprintFile) { (Get-Content -Raw -LiteralPath $FingerprintFile).Trim() } else { "" }
        $ProfilePattern = '(?i)--profile\s+' + [regex]::Escape($Profile) + '(?:\s|$)'
        $CommandMatches = $Existing.CommandLine -match [regex]::Escape($ExternalUrl) -and
            $Existing.CommandLine -match $ProfilePattern
        $Healthy = $false
        try { $Healthy = (Invoke-WebRequest -UseBasicParsing -Uri "http://127.0.0.1:18081/healthz" -TimeoutSec 3).StatusCode -eq 200 } catch { }
        if ($CommandMatches -and $StoredFingerprint -eq $ExpectedFingerprint -and $Healthy) {
            Write-Host "mcp-auth-proxy is already healthy (PID $ExistingPid)."
            return [pscustomobject]@{ Started = $false; ProcessId = $ExistingPid }
        }
        if (-not $CommandMatches -or $StoredFingerprint -ne $ExpectedFingerprint) {
            throw "A proxy from this template is already running with different URL, profile, or password settings. Run mcp-public/stop.ps1, then start again."
        }
        Stop-Process -Id $ExistingPid
        $StopDeadline = [DateTime]::UtcNow.AddSeconds(15)
        while ((Get-Process -Id $ExistingPid -ErrorAction SilentlyContinue) -and [DateTime]::UtcNow -lt $StopDeadline) { Start-Sleep -Milliseconds 250 }
        if (Get-Process -Id $ExistingPid -ErrorAction SilentlyContinue) { throw "Unhealthy MCP proxy could not be stopped safely." }
    }
    Remove-Item -LiteralPath $PidFile -Force -ErrorAction SilentlyContinue
    Remove-Item -LiteralPath $FingerprintFile -Force -ErrorAction SilentlyContinue
}

$env:PASSWORD_HASH = $PasswordHash
try {
    $QuotedDataDir = '"' + $DataDir.Replace('"', '\"') + '"'
    $QuotedWeKnora = '"' + $WeKnora.Replace('"', '\"') + '"'
    $Arguments = @(
        "--listen", "127.0.0.1:18081",
        "--external-url", $ExternalUrl,
        "--no-auto-tls",
        "--data-path", $QuotedDataDir,
        "--", $QuotedWeKnora, "--profile", $Profile, "mcp", "serve"
    )
    $Process = Start-Process -FilePath $Proxy -ArgumentList $Arguments -WorkingDirectory $Root `
        -WindowStyle Hidden -PassThru `
        -RedirectStandardOutput (Join-Path $LogDir "proxy.stdout.log") `
        -RedirectStandardError (Join-Path $LogDir "proxy.stderr.log")
} finally {
    Remove-Item Env:PASSWORD_HASH -ErrorAction SilentlyContinue
    $PasswordHash = $null
}
[IO.File]::WriteAllText($PidFile, [string]$Process.Id, [Text.UTF8Encoding]::new($false))

$Deadline = [DateTime]::UtcNow.AddSeconds(30)
do {
    if ($Process.HasExited) {
        Remove-Item -LiteralPath $PidFile -Force -ErrorAction SilentlyContinue
        Remove-Item -LiteralPath $FingerprintFile -Force -ErrorAction SilentlyContinue
        throw "mcp-auth-proxy exited. See mcp-public/logs/proxy.stderr.log."
    }
    try {
        $Health = Invoke-WebRequest -UseBasicParsing -Uri "http://127.0.0.1:18081/healthz" -TimeoutSec 3
        if ($Health.StatusCode -eq 200) {
            [IO.File]::WriteAllText($FingerprintFile, $ExpectedFingerprint, [Text.UTF8Encoding]::new($false))
            Write-Host "Local OAuth MCP endpoint is ready at http://127.0.0.1:18081/mcp (profile: $Profile)."
            return [pscustomobject]@{ Started = $true; ProcessId = $Process.Id }
        }
    } catch { }
    Start-Sleep -Seconds 1
} while ([DateTime]::UtcNow -lt $Deadline)
if (-not $Process.HasExited) {
    Stop-Process -Id $Process.Id -ErrorAction SilentlyContinue
    $StopDeadline = [DateTime]::UtcNow.AddSeconds(15)
    while ((Get-Process -Id $Process.Id -ErrorAction SilentlyContinue) -and [DateTime]::UtcNow -lt $StopDeadline) { Start-Sleep -Milliseconds 250 }
}
if (Get-Process -Id $Process.Id -ErrorAction SilentlyContinue) {
    throw "mcp-auth-proxy stayed alive but unhealthy; PID file was retained for safe manual teardown."
}
Remove-Item -LiteralPath $PidFile -Force -ErrorAction SilentlyContinue
Remove-Item -LiteralPath $FingerprintFile -Force -ErrorAction SilentlyContinue
throw "mcp-auth-proxy did not become healthy within 30 seconds."
