[CmdletBinding()]
param(
    [string]$WslDistro = "Ubuntu",
    [string]$WeKnoraVersion = "v0.7.1",
    [switch]$InstallMcpTools,
    [switch]$StartWeKnora
)

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
$Runtime = Join-Path $Root ".runtime"
$WeKnora = Join-Path $Runtime "WeKnora"
$Bin = Join-Path $Root "bin"
$WeKnoraExpectedCommit = "c64a48647cd6f7eb8b0fb020b2e8fec74ee375fb"

function Require-Command([string]$Name, [string]$InstallUrl) {
    if (-not (Get-Command $Name -ErrorAction SilentlyContinue)) {
        throw "Missing prerequisite '$Name'. Install it from $InstallUrl and run this script again."
    }
}

function Download-WithResume([string]$Url, [string]$Destination) {
    if (Test-Path $Destination) { return }
    Require-Command curl.exe "https://curl.se/windows/"
    $Partial = "$Destination.partial"
    & curl.exe -L --fail --retry 3 --retry-delay 2 --continue-at - --output $Partial $Url
    if ($LASTEXITCODE -ne 0) { throw "Download failed: $Url" }
    Move-Item -LiteralPath $Partial -Destination $Destination -Force
}

function Set-DotEnvValue([string]$Path, [string]$Name, [string]$Value) {
    $Text = if (Test-Path $Path) {
        [IO.File]::ReadAllText($Path, [Text.Encoding]::UTF8)
    } else { "" }
    $Line = "$Name=$Value"
    $Pattern = "(?m)^\s*" + [regex]::Escape($Name) + "\s*=.*$"
    if ($Text -match $Pattern) {
        $Text = [regex]::Replace($Text, $Pattern, $Line)
    } else {
        if ($Text -and -not $Text.EndsWith("`n")) { $Text += "`r`n" }
        $Text += "$Line`r`n"
    }
    [IO.File]::WriteAllText($Path, $Text, [Text.UTF8Encoding]::new($false))
}

Require-Command git "https://git-scm.com/download/win"
Require-Command go "https://go.dev/dl/"
Require-Command uv "https://docs.astral.sh/uv/getting-started/installation/"
Require-Command wsl.exe "https://learn.microsoft.com/windows/wsl/install"

$GoVersionText = (& go version) -join " "
if ($GoVersionText -notmatch 'go(\d+)\.(\d+)') {
    throw "Unable to read the installed Go version."
}
$GoMajor = [int]$Matches[1]
$GoMinor = [int]$Matches[2]
if ($GoMajor -lt 1 -or ($GoMajor -eq 1 -and $GoMinor -lt 26)) {
    throw "WeKnora CLI requires Go 1.26 or newer. Found: $GoVersionText"
}

New-Item -ItemType Directory -Force -Path $Runtime, $Bin | Out-Null
foreach ($Folder in "inbox", "archives", "work", "markdown", "failed", "outputs") {
    New-Item -ItemType Directory -Force -Path (Join-Path $Root $Folder) | Out-Null
}

if (-not (Test-Path (Join-Path $Root ".env"))) {
    Copy-Item (Join-Path $Root ".env.example") (Join-Path $Root ".env")
    Write-Host "Created .env (fill in your own provider keys)."
}
if (-not (Test-Path (Join-Path $Root "config.local.yaml"))) {
    Copy-Item (Join-Path $Root "config.example.yaml") (Join-Path $Root "config.local.yaml")
    Write-Host "Created config.local.yaml."
}

Push-Location $Root
try {
    & uv sync --locked
    if ($LASTEXITCODE -ne 0) { throw "uv sync failed." }
} finally {
    Pop-Location
}

if (-not (Test-Path (Join-Path $WeKnora ".git"))) {
    & git clone --depth 1 --branch $WeKnoraVersion https://github.com/Tencent/WeKnora.git $WeKnora
    if ($LASTEXITCODE -ne 0) { throw "WeKnora clone failed." }
} else {
    $CurrentTag = (& git -C $WeKnora describe --tags --exact-match 2>$null) -join ""
    if ($CurrentTag -ne $WeKnoraVersion) {
        throw "Existing .runtime/WeKnora is '$CurrentTag', expected '$WeKnoraVersion'. Remove it manually if you want bootstrap to clone the pinned version."
    }
}

$ActualWeKnoraCommit = ((& git -C $WeKnora rev-parse HEAD) -join "").Trim().ToLowerInvariant()
if ($ActualWeKnoraCommit -ne $WeKnoraExpectedCommit) {
    throw "WeKnora source mismatch. Expected $WeKnoraExpectedCommit for $WeKnoraVersion, found $ActualWeKnoraCommit. Remove .runtime/WeKnora manually before retrying."
}

Push-Location (Join-Path $WeKnora "cli")
try {
    $WeKnoraCommit = ((& git -C $WeKnora rev-parse --short=12 HEAD) -join "").Trim()
    $BuildDate = [DateTime]::UtcNow.ToString("yyyy-MM-ddTHH:mm:ssZ")
    $BuildPackage = "github.com/Tencent/WeKnora/cli/internal/build"
    $LdFlags = "-s -w -X $BuildPackage.Version=$WeKnoraVersion -X $BuildPackage.Commit=$WeKnoraCommit -X $BuildPackage.Date=$BuildDate"
    & go build -trimpath "-ldflags=$LdFlags" -o (Join-Path $Bin "weknora.exe") .
    if ($LASTEXITCODE -ne 0) { throw "WeKnora CLI build failed." }
} finally {
    Pop-Location
}

if ($InstallMcpTools) {
    $ProxyPath = Join-Path $Bin "mcp-auth-proxy.exe"
    $ProxyUrl = "https://github.com/sigbit/mcp-auth-proxy/releases/download/v2.10.2/mcp-auth-proxy-windows-amd64.exe"
    $ProxySha256 = "f64119236682f4f16adc025c69b1cea1c075ced24df0ed4fb52f22837c0ed3b5"
    Download-WithResume $ProxyUrl $ProxyPath
    $ActualProxyHash = (Get-FileHash -Algorithm SHA256 -LiteralPath $ProxyPath).Hash.ToLowerInvariant()
    if ($ActualProxyHash -ne $ProxySha256) {
        throw "mcp-auth-proxy SHA-256 mismatch. Delete '$ProxyPath' before retrying."
    }

    $CloudflaredPath = Join-Path $Bin "cloudflared.exe"
    $CloudflaredUrl = "https://github.com/cloudflare/cloudflared/releases/download/2026.7.3/cloudflared-windows-amd64.exe"
    $CloudflaredSha256 = "8635da433b6df8194746e88ed9d2589566c20e38bfc2a80e431a348b7c765841"
    Download-WithResume $CloudflaredUrl $CloudflaredPath
    $ActualCloudflaredHash = (Get-FileHash -Algorithm SHA256 -LiteralPath $CloudflaredPath).Hash.ToLowerInvariant()
    if ($ActualCloudflaredHash -ne $CloudflaredSha256) {
        throw "cloudflared SHA-256 mismatch. Delete '$CloudflaredPath' before retrying."
    }
    $Signature = Get-AuthenticodeSignature -LiteralPath $CloudflaredPath
    if ($Signature.Status -ne "Valid" -or $Signature.SignerCertificate.Subject -notmatch "Cloudflare") {
        throw "cloudflared signature validation failed. Delete '$CloudflaredPath' before retrying."
    }
}

$WeKnoraEnv = Join-Path $WeKnora ".env"
if (-not (Test-Path $WeKnoraEnv)) {
    Copy-Item (Join-Path $WeKnora ".env.example") $WeKnoraEnv
}
# Compose accepts host:port in these variables. Binding loopback prevents WeKnora
# from being exposed to the LAN while keeping the documented UI port stable.
Set-DotEnvValue $WeKnoraEnv "APP_PORT" "127.0.0.1:8080"
Set-DotEnvValue $WeKnoraEnv "FRONTEND_PORT" "127.0.0.1:8088"
# The checked-out source and the official container images must stay on the
# same release. Leaving this at upstream's mutable `latest` silently mixes a
# pinned CLI/source tree with a different backend after a later image pull.
Set-DotEnvValue $WeKnoraEnv "WEKNORA_VERSION" $WeKnoraVersion

# Upstream v0.7.1 uses restart: always for Redis and Neo4j. That policy can
# revive manually stopped containers when Docker Desktop starts again. This
# managed override preserves normal `compose up` behavior while making an
# explicit `compose stop` survive daemon and Windows restarts.
$ComposeOverride = Join-Path $WeKnora "docker-compose.override.yml"
$ManualStartOverride = @"
services:
  redis:
    restart: unless-stopped
  neo4j:
    restart: unless-stopped
"@
if (Test-Path $ComposeOverride) {
    $ExistingOverride = [IO.File]::ReadAllText($ComposeOverride, [Text.Encoding]::UTF8)
    if ($ExistingOverride.Trim() -ne $ManualStartOverride.Trim()) {
        throw "Existing docker-compose.override.yml is user-managed. Preserve its settings and set Redis/Neo4j restart to unless-stopped before retrying."
    }
} else {
    [IO.File]::WriteAllText(
        $ComposeOverride,
        $ManualStartOverride + "`n",
        [Text.UTF8Encoding]::new($false)
    )
}

if ($StartWeKnora) {
    $DistroNames = (& wsl.exe -l -q) -replace "`0", ""
    if ($DistroNames -notcontains $WslDistro) {
        throw "WSL distribution '$WslDistro' was not found."
    }
    & (Join-Path $PSScriptRoot "start.ps1") -WslDistro $WslDistro
}

Write-Host "Bootstrap complete."
Write-Host "Next: open http://127.0.0.1:8088, create/sign in to a local WeKnora account, then run scripts/configure-weknora.ps1."
