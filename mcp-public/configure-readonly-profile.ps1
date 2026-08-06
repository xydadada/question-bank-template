[CmdletBinding()]
param(
    [string]$HostUrl = "http://127.0.0.1:8080",
    [string]$Profile = "mcp-readonly"
)

$ErrorActionPreference = "Stop"
$Base = Split-Path -Parent $MyInvocation.MyCommand.Path
$Root = Split-Path -Parent $Base
$Cli = Join-Path $Root "bin\weknora.exe"
if (-not (Test-Path $Cli)) { throw "Run scripts/bootstrap.ps1 first." }
if ($Profile -notmatch '^[A-Za-z0-9._-]+$') { throw "Profile names may only contain letters, numbers, dot, underscore and hyphen." }

$ProfileJson = (& $Cli profile list --format json 2>$null) -join "`n"
if ($LASTEXITCODE -ne 0) { throw "Could not read WeKnora CLI profiles." }
$Profiles = $ProfileJson | ConvertFrom-Json
$Existing = @($Profiles.data) | Where-Object { $_.name -eq $Profile } | Select-Object -First 1
if ($Existing -and $Existing.host.TrimEnd('/') -ne $HostUrl.TrimEnd('/')) {
    throw "Profile '$Profile' points to '$($Existing.host)'. Remove it explicitly with 'bin\weknora.exe profile remove $Profile' before changing hosts."
}
if (-not $Existing) {
    & $Cli profile add $Profile --host $HostUrl --format json | Out-Null
    if ($LASTEXITCODE -ne 0) { throw "Could not create WeKnora profile '$Profile'." }
}

Write-Host "Create a dedicated least-privilege API key in WeKnora first. It should only read the knowledge bases you intend to expose."
$SecureKey = Read-Host "Paste that API key (input hidden)" -AsSecureString
$KeyPtr = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($SecureKey)
$KeyBytes = $null
$Process = $null
try {
    $PlainKey = [Runtime.InteropServices.Marshal]::PtrToStringBSTR($KeyPtr)
    if ([string]::IsNullOrWhiteSpace($PlainKey)) { throw "API key cannot be empty." }
    $Psi = New-Object Diagnostics.ProcessStartInfo
    $Psi.FileName = $Cli
    $Psi.Arguments = "--profile $Profile auth login --with-token --format json"
    $Psi.UseShellExecute = $false
    $Psi.RedirectStandardInput = $true
    $Psi.RedirectStandardOutput = $true
    $Psi.RedirectStandardError = $true
    $Process = [Diagnostics.Process]::Start($Psi)
    $KeyBytes = (New-Object Text.UTF8Encoding($false)).GetBytes(
        $PlainKey + [Environment]::NewLine
    )
    $Process.StandardInput.BaseStream.Write($KeyBytes, 0, $KeyBytes.Length)
    $Process.StandardInput.BaseStream.Flush()
    $Process.StandardInput.Close()
    $null = $Process.StandardOutput.ReadToEnd()
    $null = $Process.StandardError.ReadToEnd()
    $Process.WaitForExit()
    if ($Process.ExitCode -ne 0) { throw "WeKnora rejected the API key. No key value was logged." }
} finally {
    if ($KeyBytes) {
        [Array]::Clear($KeyBytes, 0, $KeyBytes.Length)
    }
    if ($Process -and -not $Process.HasExited) {
        try { $Process.StandardInput.Close() } catch { }
        try { $Process.Kill() } catch { }
    }
    if ($KeyPtr -ne [IntPtr]::Zero) { [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($KeyPtr) }
    $PlainKey = $null
    $SecureKey = $null
}

& $Cli --profile $Profile kb list --format json | Out-Null
if ($LASTEXITCODE -ne 0) { throw "The profile authenticated, but cannot list its permitted knowledge bases." }
Write-Host "Profile '$Profile' is ready. The key is held by the official CLI credential mechanism (OS keyring or its user-only fallback), not this repository."
