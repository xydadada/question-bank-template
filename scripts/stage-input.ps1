[CmdletBinding()]
param([Parameter(Mandatory = $true)][string]$SourcePath)

$ErrorActionPreference = 'Stop'
$Root = Split-Path -Parent $PSScriptRoot
$Inbox = Join-Path $Root 'inbox'
$Source = Get-Item -LiteralPath $SourcePath -ErrorAction Stop
if ($Source.Attributes -band [IO.FileAttributes]::ReparsePoint) {
    throw 'Linked files and folders cannot be imported.'
}
if ($Source.PSIsContainer) {
    $Linked = @(Get-ChildItem -LiteralPath $Source.FullName -Recurse -Force -ErrorAction Stop |
        Where-Object { $_.Attributes -band [IO.FileAttributes]::ReparsePoint } |
        Select-Object -First 1)
    if ($Linked.Count) { throw 'The selected folder contains a link. Import ordinary files only.' }
}
$InboxFull = [IO.Path]::GetFullPath($Inbox).TrimEnd('\')
$SourceFull = [IO.Path]::GetFullPath($Source.FullName)
if ($SourceFull.StartsWith($InboxFull + '\', [StringComparison]::OrdinalIgnoreCase)) {
    Write-Host 'Already in inbox. No copy needed.'
    exit 0
}
New-Item -ItemType Directory -Force -Path $Inbox | Out-Null
$Target = Join-Path $Inbox $Source.Name
if (Test-Path -LiteralPath $Target) {
    throw "A file or folder with this name already exists in inbox: $($Source.Name)"
}
Copy-Item -LiteralPath $Source.FullName -Destination $Target -Recurse -ErrorAction Stop
Write-Host "Added to inbox: $($Source.Name)"
