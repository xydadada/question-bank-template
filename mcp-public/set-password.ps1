$ErrorActionPreference = "Stop"
$Base = Split-Path -Parent $MyInvocation.MyCommand.Path
$Root = Split-Path -Parent $Base
$Python = Join-Path $Root ".venv\Scripts\python.exe"
$Hasher = Join-Path $Base "hash-password.py"
$Secrets = Join-Path $Base "secrets"
$HashFile = Join-Path $Secrets "password-hash.txt"
if (-not (Test-Path $Python)) { throw "Run scripts/bootstrap.ps1 first." }

$First = Read-Host "Enter a new MCP OAuth password (input hidden)" -AsSecureString
$Second = Read-Host "Enter it again" -AsSecureString
$FirstPtr = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($First)
$SecondPtr = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($Second)
try {
    $FirstPlain = [Runtime.InteropServices.Marshal]::PtrToStringBSTR($FirstPtr)
    $SecondPlain = [Runtime.InteropServices.Marshal]::PtrToStringBSTR($SecondPtr)
    if ($FirstPlain -ne $SecondPlain) { throw "Passwords do not match." }
    if ($FirstPlain.Length -lt 10) { throw "Use at least 10 characters." }

    $Psi = [Diagnostics.ProcessStartInfo]::new()
    $Psi.FileName = $Python
    $Psi.ArgumentList.Add($Hasher)
    $Psi.UseShellExecute = $false
    $Psi.RedirectStandardInput = $true
    $Psi.RedirectStandardOutput = $true
    $Psi.RedirectStandardError = $true
    $Process = [Diagnostics.Process]::Start($Psi)
    $Process.StandardInput.WriteLine($FirstPlain)
    $Process.StandardInput.Close()
    $Hash = $Process.StandardOutput.ReadToEnd().Trim()
    $ErrorText = $Process.StandardError.ReadToEnd()
    $Process.WaitForExit()
    if ($Process.ExitCode -ne 0 -or -not $Hash.StartsWith('$2')) {
        throw "Password hashing failed: $ErrorText"
    }
} finally {
    if ($FirstPtr -ne [IntPtr]::Zero) { [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($FirstPtr) }
    if ($SecondPtr -ne [IntPtr]::Zero) { [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($SecondPtr) }
    $FirstPlain = $null
    $SecondPlain = $null
}

New-Item -ItemType Directory -Force -Path $Secrets | Out-Null
[IO.File]::WriteAllText($HashFile, $Hash, [Text.UTF8Encoding]::new($false))
$Sid = [Security.Principal.WindowsIdentity]::GetCurrent().User.Value
& "$env:WINDIR\System32\icacls.exe" $Secrets /inheritance:r /grant:r "*${Sid}:(OI)(CI)F" "*S-1-5-18:(OI)(CI)F" | Out-Null
Write-Host "Password hash saved to an ignored, current-user-only directory."
