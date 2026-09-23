[CmdletBinding()]
param([Parameter(Mandatory = $true)][string]$Output)

$ErrorActionPreference = 'Stop'
$Root = Split-Path -Parent $PSScriptRoot
$Cli = Join-Path $Root 'bin\weknora.exe'
$Manifest = Join-Path $Root 'bin\weknora.sha256'
$ExpectedCommit = 'c64a48647cd6f7eb8b0fb020b2e8fec74ee375fb'
if (-not (Test-Path -LiteralPath $Cli)) { throw 'Build bin/weknora.exe using scripts/bootstrap.ps1 first.' }
$ActualHash = (Get-FileHash -Algorithm SHA256 -LiteralPath $Cli).Hash.ToLowerInvariant()
$ManifestText = "$ActualHash $ExpectedCommit`n"
$OutputFull = [IO.Path]::GetFullPath($Output)
if (Test-Path -LiteralPath $OutputFull) { throw 'Output archive already exists.' }
$OutputParent = Split-Path -Parent $OutputFull
if (-not (Test-Path -LiteralPath $OutputParent)) { throw 'Output directory does not exist.' }
if ((& git -C $Root status --porcelain).Count) { throw 'Package a committed, clean checkout.' }
& git -C $Root archive --format=zip --output=$OutputFull HEAD
if ($LASTEXITCODE -ne 0) { throw 'Could not archive the committed source.' }
Add-Type -AssemblyName System.IO.Compression
Add-Type -AssemblyName System.IO.Compression.FileSystem
$Zip = [IO.Compression.ZipFile]::Open($OutputFull, [IO.Compression.ZipArchiveMode]::Update)
try {
    $CliEntry = $Zip.CreateEntry('bin/weknora.exe', [IO.Compression.CompressionLevel]::Optimal)
    $InputStream = [IO.File]::OpenRead($Cli)
    $OutputStream = $CliEntry.Open()
    try { $InputStream.CopyTo($OutputStream) } finally { $OutputStream.Dispose(); $InputStream.Dispose() }
    $HashEntry = $Zip.CreateEntry('bin/weknora.sha256')
    $HashStream = $HashEntry.Open()
    $Writer = [IO.StreamWriter]::new($HashStream, [Text.UTF8Encoding]::new($false))
    try { $Writer.Write($ManifestText) } finally { $Writer.Dispose() }
} finally { $Zip.Dispose() }
Write-Host "Created Windows source-and-CLI package: $OutputFull"
