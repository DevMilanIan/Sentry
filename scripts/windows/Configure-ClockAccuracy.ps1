#Requires -RunAsAdministrator
[CmdletBinding()]
param()

# Microsoft's Windows Time high-accuracy timing recipe. This does not lower the
# application's 250ms gate or alter maximum positive/negative correction limits.
# https://learn.microsoft.com/en-us/windows-server/networking/windows-time-service/configuring-systems-for-high-accuracy
$ErrorActionPreference = 'Stop'
$taskTimeConfig = 'HKLM:\SYSTEM\CurrentControlSet\Services\W32Time\Config'
$taskNtpConfig = 'HKLM:\SYSTEM\CurrentControlSet\Services\W32Time\TimeProviders\NtpClient'
$taskBackupRoot = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..\..\var\setup'))
New-Item -ItemType Directory -Path $taskBackupRoot -Force | Out-Null
$taskBackupPath = Join-Path $taskBackupRoot ('clock-settings-' + [Guid]::NewGuid().ToString('N') + '.json')
$taskPrevious = [ordered]@{
    captured_at = [DateTime]::UtcNow.ToString('o')
    MinPollInterval = (Get-ItemPropertyValue $taskTimeConfig MinPollInterval)
    MaxPollInterval = (Get-ItemPropertyValue $taskTimeConfig MaxPollInterval)
    UpdateInterval = (Get-ItemPropertyValue $taskTimeConfig UpdateInterval)
    SpecialPollInterval = (Get-ItemPropertyValue $taskNtpConfig SpecialPollInterval)
    MaxAllowedPhaseOffset = (Get-ItemPropertyValue $taskTimeConfig MaxAllowedPhaseOffset)
    MaxNegPhaseCorrection = (Get-ItemPropertyValue $taskTimeConfig MaxNegPhaseCorrection)
    MaxPosPhaseCorrection = (Get-ItemPropertyValue $taskTimeConfig MaxPosPhaseCorrection)
}
$taskPrevious | ConvertTo-Json | Set-Content -LiteralPath $taskBackupPath -Encoding UTF8
Set-ItemProperty -LiteralPath $taskTimeConfig -Name MinPollInterval -Value 6
Set-ItemProperty -LiteralPath $taskTimeConfig -Name MaxPollInterval -Value 6
Set-ItemProperty -LiteralPath $taskTimeConfig -Name UpdateInterval -Value 100
Set-ItemProperty -LiteralPath $taskNtpConfig -Name SpecialPollInterval -Value 64
w32tm /config /update
if ($LASTEXITCODE -ne 0) { throw "Windows Time config update failed: $LASTEXITCODE" }
Restart-Service -Name W32Time
w32tm /resync /rediscover
if ($LASTEXITCODE -ne 0) { throw "Windows Time rediscovery failed: $LASTEXITCODE" }
Write-Host "High-accuracy timing applied; previous non-secret values saved at $taskBackupPath"
Write-Host 'Clock correction may slew. Independent measured offset must pass before startup.'
