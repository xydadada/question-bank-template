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
$LocalConfig = Join-Path $Root "config.local.yaml"
$HashFile = Join-Path $Base "secrets\password-hash.txt"
$DataDir = Join-Path $Base "data"
$LogDir = Join-Path $Base "logs"
$PidFile = Join-Path $Base "proxy.pid"
$FingerprintFile = Join-Path $DataDir "proxy-config.sha256"

$MutexHasher = [Security.Cryptography.SHA256]::Create()
try {
    $MutexDigest = ([BitConverter]::ToString(
        $MutexHasher.ComputeHash([Text.Encoding]::UTF8.GetBytes($Root.ToLowerInvariant()))
    )).Replace('-', '').Substring(0, 24)
} finally { $MutexHasher.Dispose() }
$StartMutex = [Threading.Mutex]::new($false, "Local\QuestionBank-$MutexDigest-McpLifecycle")
$StartMutexOwned = $false
try { $StartMutexOwned = $StartMutex.WaitOne(0) } catch [Threading.AbandonedMutexException] { $StartMutexOwned = $true }
if (-not $StartMutexOwned) {
    $StartMutex.Dispose()
    throw "Another MCP proxy start operation is already running for this template."
}

try {

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
foreach ($Required in $Proxy, $WeKnora, $HashFile, $LocalConfig) {
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

function Invoke-WeKnoraJson([string[]]$Arguments, [string]$FailureMessage) {
    # The CLI contract reserves stdout for JSON and may emit harmless notices
    # on stderr. Mixing both streams makes valid JSON impossible to parse.
    $Raw = (& $WeKnora @Arguments 2>$null) -join "`n"
    if ($LASTEXITCODE -ne 0) { throw $FailureMessage }
    try { return $Raw | ConvertFrom-Json } catch {
        throw "$FailureMessage The official CLI returned malformed JSON."
    }
}

function Stop-ManagedProxyTree([int]$RootProcessId) {
    $Rows = @(Get-CimInstance Win32_Process -ErrorAction SilentlyContinue)
    $Pending = [Collections.Generic.Stack[int]]::new()
    $Order = [Collections.Generic.List[int]]::new()
    $Pending.Push($RootProcessId)
    while ($Pending.Count) {
        $Current = $Pending.Pop()
        if ($Order.Contains($Current)) { continue }
        $Order.Add($Current)
        foreach ($Child in $Rows | Where-Object { $_.ParentProcessId -eq $Current }) {
            $Pending.Push([int]$Child.ProcessId)
        }
    }
    # Stop the supervisor first so it cannot spawn a replacement child while
    # the already captured descendants are being terminated.
    Stop-Process -Id $RootProcessId -Force -ErrorAction SilentlyContinue
    for ($Index = $Order.Count - 1; $Index -ge 0; $Index--) {
        Stop-Process -Id $Order[$Index] -Force -ErrorAction SilentlyContinue
    }
    $Deadline = [DateTime]::UtcNow.AddSeconds(15)
    do {
        $RemainingIds = @($Order | Where-Object {
            Get-Process -Id $_ -ErrorAction SilentlyContinue
        })
        if (-not $RemainingIds.Count) { return }
        Start-Sleep -Milliseconds 250
    } while ([DateTime]::UtcNow -lt $Deadline)
    throw "MCP proxy process tree still has running PIDs: $($RemainingIds -join ', ')"
}

# A listening proxy is not proof that the dedicated read-only credential can
# still reach its knowledge bases or execute vector retrieval. Validate the
# exact profile and one real hybrid search before exposing the public route.
Invoke-WeKnoraJson @(
    "doctor", "--no-cache", "--format", "json", "--profile", $Profile
) "MCP profile '$Profile' failed the WeKnora doctor check. Reconfigure or renew its dedicated read-only API key." | Out-Null
$KnowledgeBases = Invoke-WeKnoraJson @(
    "kb", "list", "--limit", "10000", "--format", "json", "--profile", $Profile
) "MCP profile '$Profile' cannot list its permitted knowledge bases."
$VisibleKnowledgeBases = @($KnowledgeBases.data) | Where-Object { $_.id }
if (-not $VisibleKnowledgeBases.Count) {
    throw "MCP profile '$Profile' has no visible knowledge base. Grant retrieve access to at least one intended knowledge base."
}
$ConfigText = [IO.File]::ReadAllText($LocalConfig, [Text.Encoding]::UTF8)
$ExpectedKnowledgeBaseIds = [Collections.Generic.List[string]]::new()
foreach ($Setting in "parent_knowledge_base", "child_knowledge_base", "raw_knowledge_base") {
    $SettingMatch = [regex]::Match(
        $ConfigText,
        '(?m)^\s*' + [regex]::Escape($Setting) + '\s*:\s*["'']?(?<id>[^\s#"'']+)'
    )
    if (-not $SettingMatch.Success -or $SettingMatch.Groups['id'].Value -match '^__.+__$') {
        throw "config.local.yaml does not contain a configured '$Setting' ID. Run scripts/configure-weknora.ps1 first."
    }
    $ExpectedKnowledgeBaseIds.Add($SettingMatch.Groups['id'].Value)
}
$VisibleIds = @($VisibleKnowledgeBases | ForEach-Object { [string]$_.id })
$UniqueExpectedIds = @($ExpectedKnowledgeBaseIds | Select-Object -Unique)
if ($UniqueExpectedIds.Count -ne 3) {
    throw "The configured parent, child, and raw knowledge-base IDs must be three distinct values."
}
$MissingKnowledgeBaseIds = @(
    $ExpectedKnowledgeBaseIds | Where-Object { $_ -notin $VisibleIds }
)
if ($MissingKnowledgeBaseIds.Count) {
    throw "MCP profile '$Profile' cannot see every configured question-bank layer. Missing knowledge-base IDs: $($MissingKnowledgeBaseIds -join ', '). Update the dedicated API key permissions and reconfigure the profile."
}
$UnexpectedKnowledgeBaseIds = @(
    $VisibleIds | Where-Object { $_ -notin $UniqueExpectedIds }
)
if ($UnexpectedKnowledgeBaseIds.Count) {
    throw "MCP profile '$Profile' can see knowledge bases outside the configured three layers. Unexpected IDs: $($UnexpectedKnowledgeBaseIds -join ', '). Restrict the dedicated API key before exposing MCP."
}
foreach ($KnowledgeBaseId in $UniqueExpectedIds) {
    $ProbeKnowledgeBase = $VisibleKnowledgeBases | Where-Object {
        [string]$_.id -eq $KnowledgeBaseId -and $_.embedding_model_id
    } | Select-Object -First 1
    if (-not $ProbeKnowledgeBase) {
        throw "Configured knowledge base '$KnowledgeBaseId' is visible but has no embedding model. Repair its model configuration before exposing MCP."
    }
    $SearchResult = Invoke-WeKnoraJson @(
        "search", "chunks", "question bank startup health probe",
        "--kb", ([string]$ProbeKnowledgeBase.id), "--limit", "1",
        "--format", "json", "--profile", $Profile
    ) "MCP profile '$Profile' could not search configured knowledge base '$KnowledgeBaseId'. Check its retrieve permission and embedding service."
    if (-not @($SearchResult.data).Count) {
        throw "Configured knowledge base '$KnowledgeBaseId' returned no retrieval result. Do not expose MCP until all three layers are indexed and searchable."
    }
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

$RecordedPid = 0
if (Test-Path $PidFile) {
    try { $RecordedPid = [int](Get-Content -Raw -LiteralPath $PidFile) } catch { $RecordedPid = 0 }
}
$ExpectedPath = [IO.Path]::GetFullPath($Proxy)
$ManagedProxies = @(Get-CimInstance Win32_Process -ErrorAction SilentlyContinue | Where-Object {
    $_.ExecutablePath -and
    [IO.Path]::GetFullPath($_.ExecutablePath).Equals($ExpectedPath, [StringComparison]::OrdinalIgnoreCase) -and
    $_.CommandLine -match '127\.0\.0\.1:18081'
})
if ($ManagedProxies.Count -gt 1 -or
    ($ManagedProxies.Count -eq 1 -and $ManagedProxies[0].ProcessId -ne $RecordedPid)) {
    throw "Found an untracked or duplicate MCP proxy for this template. Run mcp-public/stop.ps1 before starting again."
}

if (Test-Path $PidFile) {
    try { $ExistingPid = [int](Get-Content -Raw -LiteralPath $PidFile) } catch { $ExistingPid = 0 }
    $Existing = if ($ExistingPid) { Get-CimInstance Win32_Process -Filter "ProcessId=$ExistingPid" -ErrorAction SilentlyContinue } else { $null }
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
        Stop-ManagedProxyTree $ExistingPid
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
        Stop-ManagedProxyTree $Process.Id
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
    Stop-ManagedProxyTree $Process.Id
}
if (Get-Process -Id $Process.Id -ErrorAction SilentlyContinue) {
    throw "mcp-auth-proxy stayed alive but unhealthy; PID file was retained for safe manual teardown."
}
Remove-Item -LiteralPath $PidFile -Force -ErrorAction SilentlyContinue
Remove-Item -LiteralPath $FingerprintFile -Force -ErrorAction SilentlyContinue
throw "mcp-auth-proxy did not become healthy within 30 seconds."
} finally {
    if ($StartMutexOwned) { $StartMutex.ReleaseMutex() }
    $StartMutex.Dispose()
}
