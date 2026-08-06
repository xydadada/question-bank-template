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
$PasswordBytes = $null
$Process = $null
try {
    $FirstPlain = [Runtime.InteropServices.Marshal]::PtrToStringBSTR($FirstPtr)
    $SecondPlain = [Runtime.InteropServices.Marshal]::PtrToStringBSTR($SecondPtr)
    if ($FirstPlain -ne $SecondPlain) { throw "Passwords do not match." }
    if ($FirstPlain.Length -lt 10) { throw "Use at least 10 characters." }
    if ([Text.Encoding]::UTF8.GetByteCount($FirstPlain) -gt 72) {
        throw "Use at most 72 UTF-8 bytes because bcrypt cannot safely accept more."
    }

    $Psi = New-Object Diagnostics.ProcessStartInfo
    $Psi.FileName = $Python
    $Psi.Arguments = '"' + $Hasher.Replace('"', '\"') + '"'
    $Psi.UseShellExecute = $false
    $Psi.RedirectStandardInput = $true
    $Psi.RedirectStandardOutput = $true
    $Psi.RedirectStandardError = $true
    $Process = [Diagnostics.Process]::Start($Psi)
    $PasswordBytes = (New-Object Text.UTF8Encoding($false)).GetBytes(
        $FirstPlain + [Environment]::NewLine
    )
    $Process.StandardInput.BaseStream.Write($PasswordBytes, 0, $PasswordBytes.Length)
    $Process.StandardInput.BaseStream.Flush()
    $Process.StandardInput.Close()
    $Hash = $Process.StandardOutput.ReadToEnd().Trim()
    $null = $Process.StandardError.ReadToEnd()
    $Process.WaitForExit()
    if ($Process.ExitCode -ne 0 -or -not $Hash.StartsWith('$2')) {
        throw "Password hashing failed. No password value was logged."
    }
} finally {
    if ($PasswordBytes) {
        [Array]::Clear($PasswordBytes, 0, $PasswordBytes.Length)
    }
    if ($Process -and -not $Process.HasExited) {
        try { $Process.StandardInput.Close() } catch { }
        try { $Process.Kill() } catch { }
    }
    if ($FirstPtr -ne [IntPtr]::Zero) { [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($FirstPtr) }
    if ($SecondPtr -ne [IntPtr]::Zero) { [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($SecondPtr) }
    $FirstPlain = $null
    $SecondPlain = $null
}

New-Item -ItemType Directory -Force -Path $Secrets | Out-Null
$Sid = [Security.Principal.WindowsIdentity]::GetCurrent().User.Value
& "$env:WINDIR\System32\icacls.exe" $Secrets /inheritance:r /grant:r "*${Sid}:(OI)(CI)F" "*S-1-5-18:(OI)(CI)F" | Out-Null
if ($LASTEXITCODE -ne 0) { throw "Could not restrict the password-hash directory ACL." }
[IO.File]::WriteAllText($HashFile, $Hash, [Text.UTF8Encoding]::new($false))
Write-Host "Password hash saved to an ignored, current-user-only directory."
