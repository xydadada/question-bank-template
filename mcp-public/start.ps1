[CmdletBinding()]
param([string]$ExternalUrl = $env:MCP_EXTERNAL_URL)

$ErrorActionPreference = "Stop"
$Base = Split-Path -Parent $MyInvocation.MyCommand.Path
$Root = Split-Path -Parent $Base
$Proxy = Join-Path $Root "bin\mcp-auth-proxy.exe"
$WeKnora = Join-Path $Root "bin\weknora.exe"
$HashFile = Join-Path $Base "secrets\password-hash.txt"
$DataDir = Join-Path $Base "data"
$LogDir = Join-Path $Base "logs"
$PidFile = Join-Path $Base "proxy.pid"

if (-not $ExternalUrl) { throw "Set MCP_EXTERNAL_URL or pass -ExternalUrl." }
if ($ExternalUrl -eq "https://mcp.example.com") { throw "Replace the example MCP URL first." }
foreach ($Required in $Proxy, $WeKnora, $HashFile) {
    if (-not (Test-Path $Required -PathType Leaf)) { throw "Missing required file: $Required" }
}
if (Test-Path $PidFile) {
    $ExistingPid = [int](Get-Content -Raw $PidFile)
    if (Get-Process -Id $ExistingPid -ErrorAction SilentlyContinue) {
        Write-Host "mcp-auth-proxy is already running (PID $ExistingPid)."
        return
    }
    Remove-Item $PidFile -Force
}

New-Item -ItemType Directory -Force -Path $DataDir, $LogDir | Out-Null
$Sid = [Security.Principal.WindowsIdentity]::GetCurrent().User.Value
foreach ($Directory in $DataDir, (Split-Path -Parent $HashFile)) {
    & "$env:WINDIR\System32\icacls.exe" $Directory /inheritance:r /grant:r "*${Sid}:(OI)(CI)F" "*S-1-5-18:(OI)(CI)F" | Out-Null
}

$env:PASSWORD_HASH = (Get-Content -Raw $HashFile).Trim()
try {
    $Arguments = @(
        "--listen", "127.0.0.1:18081",
        "--external-url", $ExternalUrl,
        "--no-auto-tls",
        "--data-path", $DataDir,
        "--", $WeKnora, "--profile", "local", "mcp", "serve"
    )
    $Process = Start-Process -FilePath $Proxy -ArgumentList $Arguments -WorkingDirectory $Root `
        -WindowStyle Hidden -PassThru `
        -RedirectStandardOutput (Join-Path $LogDir "proxy.stdout.log") `
        -RedirectStandardError (Join-Path $LogDir "proxy.stderr.log")
} finally {
    Remove-Item Env:PASSWORD_HASH -ErrorAction SilentlyContinue
}
[IO.File]::WriteAllText($PidFile, [string]$Process.Id, [Text.UTF8Encoding]::new($false))
Start-Sleep -Seconds 2
if ($Process.HasExited) {
    Remove-Item $PidFile -Force -ErrorAction SilentlyContinue
    throw "mcp-auth-proxy exited. See mcp-public/logs/proxy.stderr.log."
}
Write-Host "Local OAuth MCP endpoint started at http://127.0.0.1:18081/mcp."
