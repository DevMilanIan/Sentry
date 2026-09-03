#Requires -RunAsAdministrator
[CmdletBinding(SupportsShouldProcess)]
param()

$ErrorActionPreference = 'Stop'
$features = @('Microsoft-Windows-Subsystem-Linux', 'VirtualMachinePlatform')
foreach ($feature in $features) {
    $current = Get-WindowsOptionalFeature -Online -FeatureName $feature
    if ($current.State -ne 'Enabled' -and $PSCmdlet.ShouldProcess($feature, 'Enable Windows feature')) {
        Enable-WindowsOptionalFeature -Online -FeatureName $feature -All -NoRestart
    }
}

Write-Host 'Windows features requested. Reboot before installing/updating the WSL distribution.'
Write-Host 'After reboot: wsl --update; wsl --install -d Ubuntu; wsl --set-default-version 2'

