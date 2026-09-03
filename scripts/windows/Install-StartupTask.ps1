#Requires -RunAsAdministrator
[CmdletBinding(SupportsShouldProcess)]
param(
    [string]$ProjectDirectory = (Resolve-Path (Join-Path $PSScriptRoot '..\..')).Path,
    [string]$TaskName = 'Options Sentinel (Fail-Closed)'
)

$ErrorActionPreference = 'Stop'
$startupScript = Join-Path $ProjectDirectory 'scripts\windows\Start-Sentinel.ps1'
if (-not (Test-Path -LiteralPath $startupScript -PathType Leaf)) {
    throw "Startup script not found: $startupScript"
}
$action = New-ScheduledTaskAction -Execute 'powershell.exe' -Argument (
    "-NoProfile -NonInteractive -ExecutionPolicy RemoteSigned -File `"$startupScript`" -ProjectDirectory `"$ProjectDirectory`""
)
$trigger = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME
$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -MultipleInstances IgnoreNew -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 2)
$principal = New-ScheduledTaskPrincipal -UserId $env:USERNAME -LogonType Interactive -RunLevel Limited

if ($PSCmdlet.ShouldProcess($TaskName, 'Register fail-closed startup task')) {
    Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger -Settings $settings -Principal $principal -Force
}

