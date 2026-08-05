[CmdletBinding()]
param([Parameter(Mandatory = $true)][string]$ExternalUrl)

& (Join-Path $PSScriptRoot "start.ps1") -ExternalUrl $ExternalUrl
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
& (Join-Path $PSScriptRoot "start-cloudflare.ps1")
