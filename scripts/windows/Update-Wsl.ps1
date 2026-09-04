#Requires -RunAsAdministrator
[CmdletBinding()]
param()

$ErrorActionPreference = 'Stop'
if (-not (Get-Command winget -ErrorAction SilentlyContinue)) {
    throw 'Microsoft App Installer / winget is required for this WSL package update.'
}
winget upgrade --id Microsoft.WSL --exact --source winget --silent --accept-package-agreements --accept-source-agreements --disable-interactivity
if ($LASTEXITCODE -ne 0) {
    throw "WSL package update failed with exit code $LASTEXITCODE"
}
Write-Host 'WSL package updated. Reboot if Windows feature changes are pending before starting distributions.'
