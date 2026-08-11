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
    $ConfigText = [IO.File]::ReadAllText($LocalConfig, [Text.Encoding]::UTF8)
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
    $Expected = [IO.Path]::GetFullPath($ExpectedExecutable)
    $Managed = @(Get-CimInstance Win32_Process -ErrorAction SilentlyContinue | Where-Object {
        $_.ExecutablePath -and
        [IO.Path]::GetFullPath($_.ExecutablePath).Equals($Expected, [StringComparison]::OrdinalIgnoreCase) -and
        (!$CommandPattern -or $_.CommandLine -match $CommandPattern)
    })
    if ($Managed.Count -gt 1) {
        throw "Multiple managed processes match '$CommandPattern'. Run scripts/stop.ps1 before starting again."
    }
    $SavedPid = 0
    if (Test-Path $PidFile) {
        try { $SavedPid = [int](Get-Content -Raw -LiteralPath $PidFile) } catch { $SavedPid = 0 }
    }
    if ($Managed.Count -eq 1) {
        $Row = $Managed[0]
        if ($SavedPid -ne $Row.ProcessId) {
            [IO.File]::WriteAllText($PidFile, [string]$Row.ProcessId, [Text.UTF8Encoding]::new($false))
        }
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

function Stop-ProcessTree([int]$RootProcessId, [int]$TimeoutSeconds = 15) {
    $Known = [Collections.Generic.HashSet[int]]::new()
    $Known.Add($RootProcessId) | Out-Null
    $Discover = {
        param($Rows, $KnownIds)
        $Changed = $true
        while ($Changed) {
            $Changed = $false
            foreach ($Child in $Rows) {
                if ($KnownIds.Contains([int]$Child.ParentProcessId) -and
                    -not $KnownIds.Contains([int]$Child.ProcessId)) {
                    $KnownIds.Add([int]$Child.ProcessId) | Out-Null
                    $Changed = $true
                }
            }
        }
    }
    $Rows = @(Get-CimInstance Win32_Process -ErrorAction Stop)
    & $Discover $Rows $Known
    # Stop the supervisor first so it cannot replace a worker while the
    # descendants are being collected and terminated.
    Stop-Process -Id $RootProcessId -Force -ErrorAction SilentlyContinue
    $Deadline = [DateTime]::UtcNow.AddSeconds($TimeoutSeconds)
    do {
        $Rows = @(Get-CimInstance Win32_Process -ErrorAction Stop)
        & $Discover $Rows $Known
        $Remaining = @($Rows | Where-Object {
            $Known.Contains([int]$_.ProcessId)
        })
        foreach ($ProcessRow in $Remaining) {
            Stop-Process -Id $ProcessRow.ProcessId -Force -ErrorAction SilentlyContinue
        }
        Start-Sleep -Milliseconds 250
        $Rows = @(Get-CimInstance Win32_Process -ErrorAction Stop)
        & $Discover $Rows $Known
        $Remaining = @($Rows | Where-Object {
            $Known.Contains([int]$_.ProcessId)
        })
        if (-not $Remaining.Count) { return }
    } while ([DateTime]::UtcNow -lt $Deadline)
    $RemainingIds = @($Remaining | ForEach-Object { $_.ProcessId })
    throw "Process tree still has running PIDs: $($RemainingIds -join ', ')"
}

function Stop-RollbackProcess([string]$PidFile, [string]$ExpectedExecutable, [string]$Pattern) {
    $Row = Get-ManagedProcess $PidFile $ExpectedExecutable $Pattern
    if (-not $Row) { return }
    try {
        Stop-ProcessTree ([int]$Row.ProcessId)
    } catch {
        Write-Warning "Rollback could not stop PID $($Row.ProcessId) and its descendants; its PID file was retained: $($_.Exception.Message)"
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
$MutexHasher = [Security.Cryptography.SHA256]::Create()
try {
    $MutexDigest = ([BitConverter]::ToString(
        $MutexHasher.ComputeHash([Text.Encoding]::UTF8.GetBytes($Root.ToLowerInvariant()))
    )).Replace('-', '').Substring(0, 24)
} finally { $MutexHasher.Dispose() }
$KeepAliveToken = "question-bank-$MutexDigest"
$StartMutex = [Threading.Mutex]::new($false, "Local\QuestionBank-$MutexDigest-StackLifecycle")
$StartMutexOwned = $false
try { $StartMutexOwned = $StartMutex.WaitOne(0) } catch [Threading.AbandonedMutexException] { $StartMutexOwned = $true }
if (-not $StartMutexOwned) {
    $StartMutex.Dispose()
    throw "Another stack start operation is already running for this template."
}
$KeepAlivePid = Join-Path $Runtime "wsl-keepalive.pid"
$DistroFile = Join-Path $Runtime "wsl-distro.txt"
$PreviousDistro = if (Test-Path $DistroFile) { (Get-Content -Raw -Encoding UTF8 -LiteralPath $DistroFile).Trim() } else { "" }
$DistroPattern = [regex]::Escape($WslDistro)
$KeepAliveIdentityPattern = '(?i)QUESTION_BANK_KEEPALIVE=' + [regex]::Escape($KeepAliveToken) + '.*sleep\s+infinity'
$KeepAlivePattern = '(?i)-d\s+"?' + $DistroPattern + '"?.*' + $KeepAliveIdentityPattern
$KeepAliveStarted = $false
$ComposeUpAttempted = $false
$InitialComposeServices = @()
$NewlyStartedComposeServices = @()
$IngestStarted = $false
$ProxyStarted = $false
$TunnelStarted = $false
$Python = Join-Path $Root ".venv\Scripts\python.exe"
$IngestScript = Join-Path $Root "ingest.py"
$LocalConfigPath = Join-Path $Root "config.local.yaml"
$IngestPidFile = Join-Path $Runtime "ingest.pid"
$ProxyPidFile = Join-Path $Root "mcp-public\proxy.pid"
$TunnelPidFile = Join-Path $Root "mcp-public\cloudflared.pid"
$ProxyExe = Join-Path $Root "bin\mcp-auth-proxy.exe"
$CloudflaredExe = Join-Path $Root "bin\cloudflared.exe"
$CloudflaredConfig = Join-Path $Root "mcp-public\cloudflare\config.yml"
$TunnelPattern = '(?i)tunnel.*' + [regex]::Escape($CloudflaredConfig) + '.*run'

try {
    $ExpectedWslPath = [IO.Path]::GetFullPath($WslExe)
    $WrongDistroKeepAlive = @(Get-CimInstance Win32_Process -ErrorAction SilentlyContinue | Where-Object {
        $_.ExecutablePath -and
        [IO.Path]::GetFullPath($_.ExecutablePath).Equals($ExpectedWslPath, [StringComparison]::OrdinalIgnoreCase) -and
        $_.CommandLine -match $KeepAliveIdentityPattern -and
        $_.CommandLine -notmatch $KeepAlivePattern
    })
    if ($WrongDistroKeepAlive.Count) {
        $RecordedDistro = if ($PreviousDistro) { $PreviousDistro } else { "another distro" }
        throw "This template is still keeping WSL distro '$RecordedDistro' alive. Run scripts/stop.ps1 -StopWeKnora before switching distros."
    }
    # Persist the requested distro only after proving that this template does
    # not still own a keepalive for a different distro.  A failed switch must
    # not corrupt the stop script's last-known distro.
    [IO.File]::WriteAllText($DistroFile, $WslDistro, [Text.UTF8Encoding]::new($false))
    $KeepAlive = Get-ManagedProcess $KeepAlivePid $WslExe $KeepAlivePattern
    if (-not $KeepAlive) {
        $KeepAliveProcess = Start-Process -FilePath $WslExe `
            -ArgumentList @("-d", $WslDistro, "--", "env", "QUESTION_BANK_KEEPALIVE=$KeepAliveToken", "sleep", "infinity") `
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
        $IngestPattern = '(?i)' + [regex]::Escape($IngestScript) + '.*--supervise'
        $Existing = Get-ManagedProcess $IngestPidFile $Python $IngestPattern
        if (-not $Existing) {
            $LogDir = Join-Path $Runtime "logs"
            New-Item -ItemType Directory -Force -Path $LogDir | Out-Null
            $Process = Start-Process -FilePath $Python `
                -ArgumentList @("`"$IngestScript`"", "--supervise", "--config", "`"$LocalConfigPath`"") `
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
    if ($IngestStarted) { Stop-RollbackProcess $IngestPidFile $Python $IngestPattern }
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
} finally {
    if ($StartMutexOwned) { $StartMutex.ReleaseMutex() }
    $StartMutex.Dispose()
}
