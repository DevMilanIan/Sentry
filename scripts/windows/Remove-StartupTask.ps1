#Requires -RunAsAdministrator
[CmdletBinding(SupportsShouldProcess)]
param(
    [string]$TaskName = 'Options Sentinel (Fail-Closed)'
)

if (Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue) {
    if ($PSCmdlet.ShouldProcess($TaskName, 'Unregister startup task')) {
        Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
    }
}
