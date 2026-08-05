[CmdletBinding()]
param(
    [string]$WslDistro = "Ubuntu",
    [switch]$Processing,
    [switch]$Mcp,
    [string]$McpExternalUrl
)

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
$WeKnora = Join-Path $Root ".runtime\WeKnora"
$Cli = Join-Path $Root "bin\weknora.exe"
if (-not (Test-Path $WeKnora)) { throw "Run scripts/bootstrap.ps1 first." }

$WslPath = ((& wsl.exe -d $WslDistro -- wslpath -a $WeKnora) -join "").Trim()
if (-not $WslPath) { throw "Could not translate the WeKnora path for WSL." }
if ($WslPath.Contains("'")) { throw "Repository path cannot contain a single quote." }
& wsl.exe -d $WslDistro -- bash -lc "cd '$WslPath' && docker compose up -d"
if ($LASTEXITCODE -ne 0) { throw "WeKnora startup failed." }

if (Test-Path $Cli) {
    & $Cli doctor --format json --profile local
}

if ($Processing) {
    $PidFile = Join-Path $Root ".runtime\ingest.pid"
    $Existing = $null
    if (Test-Path $PidFile) {
        $ExistingId = [int](Get-Content -Raw $PidFile)
        $Existing = Get-Process -Id $ExistingId -ErrorAction SilentlyContinue
    }
    if (-not $Existing) {
        $LogDir = Join-Path $Root ".runtime\logs"
        New-Item -ItemType Directory -Force -Path $LogDir | Out-Null
        $Process = Start-Process -FilePath "uv" `
            -ArgumentList @("run", "python", "ingest.py", "--supervise", "--config", "config.local.yaml") `
            -WorkingDirectory $Root -WindowStyle Hidden -PassThru `
            -RedirectStandardOutput (Join-Path $LogDir "ingest.stdout.log") `
            -RedirectStandardError (Join-Path $LogDir "ingest.stderr.log")
        [IO.File]::WriteAllText($PidFile, [string]$Process.Id, [Text.UTF8Encoding]::new($false))
    }
}

if ($Mcp) {
    if (-not $McpExternalUrl) { throw "Pass -McpExternalUrl https://your-hostname when using -Mcp." }
    & (Join-Path $Root "mcp-public\start-all.ps1") -ExternalUrl $McpExternalUrl
}
Write-Host "Requested services started. Processing=$Processing MCP=$Mcp"
