$ErrorActionPreference = "Stop"
$Base = Split-Path -Parent $MyInvocation.MyCommand.Path
$Root = Split-Path -Parent $Base
$Cloudflared = Join-Path $Root "bin\cloudflared.exe"
$Config = Join-Path $Base "cloudflare\config.yml"
$LogDir = Join-Path $Base "logs"
$PidFile = Join-Path $Base "cloudflared.pid"
foreach ($Required in $Cloudflared, $Config) {
    if (-not (Test-Path $Required -PathType Leaf)) { throw "Missing required file: $Required" }
}
if (Test-Path $PidFile) {
    $ExistingPid = [int](Get-Content -Raw $PidFile)
    if (Get-Process -Id $ExistingPid -ErrorAction SilentlyContinue) {
        Write-Host "cloudflared is already running (PID $ExistingPid)."
        return
    }
    Remove-Item $PidFile -Force
}
New-Item -ItemType Directory -Force -Path $LogDir | Out-Null
$Process = Start-Process -FilePath $Cloudflared -ArgumentList @("tunnel", "--config", $Config, "run") `
    -WorkingDirectory $Root -WindowStyle Hidden -PassThru `
    -RedirectStandardOutput (Join-Path $LogDir "cloudflared.stdout.log") `
    -RedirectStandardError (Join-Path $LogDir "cloudflared.stderr.log")
[IO.File]::WriteAllText($PidFile, [string]$Process.Id, [Text.UTF8Encoding]::new($false))
Start-Sleep -Seconds 3
if ($Process.HasExited) {
    Remove-Item $PidFile -Force -ErrorAction SilentlyContinue
    throw "cloudflared exited. See mcp-public/logs/cloudflared.stderr.log."
}
Write-Host "Cloudflare Tunnel started."
