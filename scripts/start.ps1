[CmdletBinding()]
param(
    [string]$WslDistro = "Ubuntu",
    [switch]$Processing,
    [switch]$Mcp,
    [string]$McpExternalUrl
)

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
$Runtime = Join-Path $Root ".runtime"
$WeKnora = Join-Path $Runtime "WeKnora"
$Cli = Join-Path $Root "bin\weknora.exe"
$WslExe = Join-Path $env:WINDIR "System32\wsl.exe"
$RuntimeProfile = "local"
$McpProfile = "mcp-readonly"
$LocalConfig = Join-Path $Root "config.local.yaml"
if (Test-Path $LocalConfig) {
    $ConfigText = Get-Content -Raw -LiteralPath $LocalConfig
    $ProfileMatch = [regex]::Match($ConfigText, '(?m)^  profile:\s*["'']?([^\s#"'']+)')
    if ($ProfileMatch.Success) { $RuntimeProfile = $ProfileMatch.Groups[1].Value }
    $McpSection = [regex]::Match($ConfigText, '(?ms)^mcp_public:\s*\r?\n(?<body>(?:^[ \t]+[^\r\n]*(?:\r?\n|$))*)')
    if ($McpSection.Success) {
        $McpProfileMatch = [regex]::Match($McpSection.Groups['body'].Value, '(?m)^  weknora_profile:\s*["'']?(?<value>[^#\r\n"'']+)')
        if ($McpProfileMatch.Success) { $McpProfile = $McpProfileMatch.Groups['value'].Value.Trim() }
        if (-not $McpExternalUrl) {
            $McpUrlMatch = [regex]::Match($McpSection.Groups['body'].Value, '(?m)^  external_url:\s*["'']?(?<value>[^#\r\n"'']+)')
            if ($McpUrlMatch.Success) { $McpExternalUrl = $McpUrlMatch.Groups['value'].Value.Trim() }
        }
    }
}
if (-not (Test-Path (Join-Path $WeKnora "docker-compose.yml"))) {
    throw "Run scripts/bootstrap.ps1 first."
}

function Get-ManagedProcess(
    [string]$PidFile,
    [string]$ExpectedExecutable,
    [string]$CommandPattern
) {
    if (-not (Test-Path $PidFile)) { return $null }
    try { $SavedPid = [int](Get-Content -Raw -LiteralPath $PidFile) } catch {
        Remove-Item -LiteralPath $PidFile -Force -ErrorAction SilentlyContinue
        return $null
    }
    $Row = Get-CimInstance Win32_Process -Filter "ProcessId=$SavedPid" -ErrorAction SilentlyContinue
    $Expected = [IO.Path]::GetFullPath($ExpectedExecutable)
    if ($Row -and $Row.ExecutablePath -and
        [IO.Path]::GetFullPath($Row.ExecutablePath).Equals($Expected, [StringComparison]::OrdinalIgnoreCase) -and
        (!$CommandPattern -or $Row.CommandLine -match $CommandPattern)) {
        return $Row
    }
    Remove-Item -LiteralPath $PidFile -Force -ErrorAction SilentlyContinue
    return $null
}

function Wait-Http([string]$Name, [string]$Uri, [int]$TimeoutSeconds = 90) {
    $Deadline = [DateTime]::UtcNow.AddSeconds($TimeoutSeconds)
    do {
        try {
            $Response = Invoke-WebRequest -UseBasicParsing -Uri $Uri -TimeoutSec 5
            if ($Response.StatusCode -ge 200 -and $Response.StatusCode -lt 400) {
                Write-Host "$Name ready: HTTP $($Response.StatusCode)"
                return
            }
        } catch { }
        Start-Sleep -Seconds 2
    } while ([DateTime]::UtcNow -lt $Deadline)
    throw "$Name did not become ready within $TimeoutSeconds seconds: $Uri"
}

function Stop-RollbackProcess([string]$PidFile, [string]$ExpectedExecutable, [string]$Pattern) {
    $Row = Get-ManagedProcess $PidFile $ExpectedExecutable $Pattern
    if (-not $Row) { return }
    Stop-Process -Id $Row.ProcessId -ErrorAction SilentlyContinue
    $Deadline = [DateTime]::UtcNow.AddSeconds(10)
    while ((Get-Process -Id $Row.ProcessId -ErrorAction SilentlyContinue) -and [DateTime]::UtcNow -lt $Deadline) {
        Start-Sleep -Milliseconds 250
    }
    if (Get-Process -Id $Row.ProcessId -ErrorAction SilentlyContinue) {
        Write-Warning "Rollback could not stop PID $($Row.ProcessId); its PID file was retained."
        return
    }
    Remove-Item -LiteralPath $PidFile -Force -ErrorAction SilentlyContinue
}

function Get-ActiveComposeServices {
    $Output = & $WslExe -d $WslDistro --cd $WeKnora -- docker compose ps --services `
        --status running --status restarting --status paused 2>$null
    if ($LASTEXITCODE -ne 0) { throw "Could not inspect the current WeKnora Compose state." }
    return @($Output | ForEach-Object { $_.Trim() } | Where-Object { $_ })
}

New-Item -ItemType Directory -Force -Path $Runtime | Out-Null
$KeepAlivePid = Join-Path $Runtime "wsl-keepalive.pid"
$DistroFile = Join-Path $Runtime "wsl-distro.txt"
$PreviousDistro = if (Test-Path $DistroFile) { (Get-Content -Raw -LiteralPath $DistroFile).Trim() } else { "" }
if ($PreviousDistro -and -not $PreviousDistro.Equals($WslDistro, [StringComparison]::OrdinalIgnoreCase) -and
    (Test-Path $KeepAlivePid)) {
    try { $PreviousPid = [int](Get-Content -Raw -LiteralPath $KeepAlivePid) } catch { $PreviousPid = 0 }
    $PreviousKeepAlive = if ($PreviousPid) { Get-CimInstance Win32_Process -Filter "ProcessId=$PreviousPid" -ErrorAction SilentlyContinue } else { $null }
    if ($PreviousKeepAlive -and $PreviousKeepAlive.CommandLine -match '(?i)sleep\s+infinity') {
        throw "This template is still keeping WSL distro '$PreviousDistro' alive. Stop it before switching to '$WslDistro'."
    }
}
[IO.File]::WriteAllText($DistroFile, $WslDistro, [Text.UTF8Encoding]::new($false))
$DistroPattern = [regex]::Escape($WslDistro)
$KeepAlivePattern = '(?i)-d\s+"?' + $DistroPattern + '"?.*sleep\s+infinity'
$KeepAliveStarted = $false
$ComposeUpAttempted = $false
$InitialComposeServices = @()
$NewlyStartedComposeServices = @()
$IngestStarted = $false
$ProxyStarted = $false
$TunnelStarted = $false
$Python = Join-Path $Root ".venv\Scripts\python.exe"
$IngestPidFile = Join-Path $Runtime "ingest.pid"
$ProxyPidFile = Join-Path $Root "mcp-public\proxy.pid"
$TunnelPidFile = Join-Path $Root "mcp-public\cloudflared.pid"
$ProxyExe = Join-Path $Root "bin\mcp-auth-proxy.exe"
$CloudflaredExe = Join-Path $Root "bin\cloudflared.exe"
$CloudflaredConfig = Join-Path $Root "mcp-public\cloudflare\config.yml"
$TunnelPattern = '(?i)tunnel.*' + [regex]::Escape($CloudflaredConfig) + '.*run'

try {
    $KeepAlive = Get-ManagedProcess $KeepAlivePid $WslExe $KeepAlivePattern
    if (-not $KeepAlive) {
        $KeepAliveProcess = Start-Process -FilePath $WslExe `
            -ArgumentList @("-d", $WslDistro, "--", "sleep", "infinity") `
            -WindowStyle Hidden -PassThru
        [IO.File]::WriteAllText($KeepAlivePid, [string]$KeepAliveProcess.Id, [Text.UTF8Encoding]::new($false))
        $KeepAliveStarted = $true
        Write-Host "WSL keepalive started for this manual session (PID $($KeepAliveProcess.Id))."
    }

    $InitialComposeServices = @(Get-ActiveComposeServices)
    $ComposeUpAttempted = $true
    & $WslExe -d $WslDistro --cd $WeKnora -- docker compose up -d
    if ($LASTEXITCODE -ne 0) { throw "WeKnora startup failed." }
    $CurrentComposeServices = @(Get-ActiveComposeServices)
    $NewlyStartedComposeServices = @($CurrentComposeServices | Where-Object { $InitialComposeServices -notcontains $_ })

    Wait-Http "WeKnora API" "http://127.0.0.1:8080/health"
    Wait-Http "WeKnora UI" "http://127.0.0.1:8088"
    if ($Processing -or $Mcp) {
        Wait-Http "Ollama embedding service" "http://127.0.0.1:11434/api/tags"
    }

    if (Test-Path $Cli) {
        & $Cli doctor --format json --profile $RuntimeProfile
        if ($LASTEXITCODE -ne 0) {
            if ($Processing -or $Mcp) { throw "WeKnora CLI doctor failed for profile '$RuntimeProfile'. Run scripts/configure-weknora.ps1." }
            Write-Warning "WeKnora is running, but CLI profile '$RuntimeProfile' is not configured yet."
        }
    } elseif ($Processing -or $Mcp) {
        throw "WeKnora CLI is missing. Run scripts/bootstrap.ps1 first."
    }

    if ($Processing) {
        if (-not (Test-Path $Python)) { throw "Missing .venv. Run scripts/bootstrap.ps1 first." }
        $IngestPattern = "(?i)ingest\.py.*--supervise"
        $Existing = Get-ManagedProcess $IngestPidFile $Python $IngestPattern
        if (-not $Existing) {
            $LogDir = Join-Path $Runtime "logs"
            New-Item -ItemType Directory -Force -Path $LogDir | Out-Null
            $Process = Start-Process -FilePath $Python `
                -ArgumentList @("ingest.py", "--supervise", "--config", "config.local.yaml") `
                -WorkingDirectory $Root -WindowStyle Hidden -PassThru `
                -RedirectStandardOutput (Join-Path $LogDir "ingest.stdout.log") `
                -RedirectStandardError (Join-Path $LogDir "ingest.stderr.log")
            [IO.File]::WriteAllText($IngestPidFile, [string]$Process.Id, [Text.UTF8Encoding]::new($false))
            $IngestStarted = $true
            Start-Sleep -Seconds 3
            $Process.Refresh()
            if ($Process.HasExited) {
                Remove-Item -LiteralPath $IngestPidFile -Force -ErrorAction SilentlyContinue
                throw "Ingestion supervisor exited during startup. See .runtime/logs/ingest.stderr.log."
            }
            Write-Host "Ingestion supervisor is running (PID $($Process.Id))."
        } else {
            Write-Host "Ingestion supervisor is already running (PID $($Existing.ProcessId))."
        }
    }

    if ($Mcp) {
        if (-not $McpExternalUrl -or $McpExternalUrl -eq "https://mcp.example.com") {
            throw "Set mcp_public.external_url in config.local.yaml or pass -McpExternalUrl https://your-hostname."
        }
        $ProxyResult = & (Join-Path $Root "mcp-public\start.ps1") -ExternalUrl $McpExternalUrl -Profile $McpProfile
        $ProxyStarted = [bool]$ProxyResult.Started
        $TunnelResult = & (Join-Path $Root "mcp-public\start-cloudflare.ps1") -ExternalUrl $McpExternalUrl
        $TunnelStarted = [bool]$TunnelResult.Started
    }

    Write-Host "Requested services are ready. Processing=$Processing MCP=$Mcp"
} catch {
    if ($TunnelStarted) { Stop-RollbackProcess $TunnelPidFile $CloudflaredExe $TunnelPattern }
    if ($ProxyStarted) { Stop-RollbackProcess $ProxyPidFile $ProxyExe '127\.0\.0\.1:18081' }
    if ($IngestStarted) { Stop-RollbackProcess $IngestPidFile $Python '(?i)ingest\.py.*--supervise' }
    if ($ComposeUpAttempted -and -not $NewlyStartedComposeServices.Count) {
        try {
            $CurrentComposeServices = @(Get-ActiveComposeServices)
            $NewlyStartedComposeServices = @($CurrentComposeServices | Where-Object { $InitialComposeServices -notcontains $_ })
        } catch {
            Write-Warning "Could not determine which Compose services were started; no pre-existing service will be stopped automatically."
        }
    }
    if ($NewlyStartedComposeServices.Count) {
        & $WslExe -d $WslDistro --cd $WeKnora -- docker compose stop $NewlyStartedComposeServices 2>$null | Out-Null
        if ($LASTEXITCODE -ne 0) { Write-Warning "Rollback could not stop every newly started Compose service." }
    }
    if ($KeepAliveStarted) { Stop-RollbackProcess $KeepAlivePid $WslExe $KeepAlivePattern }
    throw
}
