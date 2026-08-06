[CmdletBinding()]
param(
    [string]$ExternalUrl,
    [string]$WslDistro = "Ubuntu"
)

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
$StartArguments = @{ WslDistro = $WslDistro; Mcp = $true }
if ($ExternalUrl) { $StartArguments.McpExternalUrl = $ExternalUrl }
& (Join-Path $Root "scripts\start.ps1") @StartArguments
