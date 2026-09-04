#Requires -Version 7.0
[CmdletBinding(SupportsShouldProcess)]
param(
    [string]$ProjectDirectory = (Resolve-Path (Join-Path $PSScriptRoot '..\..')).Path,
    [string]$TaskName = 'Options Sentinel (Fail-Closed)'
)

$ErrorActionPreference = 'Stop'
if ([string]::IsNullOrWhiteSpace($TaskName) -or $TaskName.IndexOfAny([char[]]'*?[]\/') -ge 0) {
    throw 'Use one exact task name without wildcard characters or a task path.'
}
$ProjectDirectory = (Resolve-Path -LiteralPath $ProjectDirectory).Path
$startupScript = Join-Path $ProjectDirectory 'scripts\windows\Start-LocalStack.ps1'
if (-not (Test-Path -LiteralPath $startupScript -PathType Leaf)) {
    throw "Startup script not found: $startupScript"
}
if ($ProjectDirectory.Contains('"') -or $ProjectDirectory.Contains("`n") -or $ProjectDirectory.Contains("`r")) {
    throw 'The project directory cannot be safely quoted for a scheduled task.'
}
$powershellCandidates = @((Join-Path $env:ProgramFiles 'PowerShell\7\pwsh.exe'))
$currentRuntime = (Get-Process -Id $PID).Path
if ($currentRuntime -and [IO.Path]::GetFileName($currentRuntime) -ieq 'pwsh.exe') {
    $powershellCandidates += $currentRuntime
}
$powershellExecutable = $null
foreach ($candidate in $powershellCandidates) {
    if (-not (Test-Path -LiteralPath $candidate -PathType Leaf)) { continue }
    $signature = Get-AuthenticodeSignature -LiteralPath $candidate
    if ($signature.Status -eq 'Valid' -and $signature.SignerCertificate -and
        $signature.SignerCertificate.Subject -match '(?:^|,\s*)O=Microsoft Corporation(?:,|$)') {
        $powershellExecutable = (Resolve-Path -LiteralPath $candidate).Path
        break
    }
}
if (-not $powershellExecutable) { throw 'A verified Microsoft-signed PowerShell 7 runtime is required.' }
$arguments = "-NoProfile -NonInteractive -WindowStyle Hidden -ExecutionPolicy RemoteSigned -File `"$startupScript`" -ProjectDirectory `"$ProjectDirectory`""
$currentIdentity = [Security.Principal.WindowsIdentity]::GetCurrent()
$currentSid = $currentIdentity.User.Value
$description = 'Options Sentinel local dependency readiness and fail-closed DEMO logon startup.'
$existing = Get-ScheduledTask -TaskName $TaskName -TaskPath '\' -ErrorAction SilentlyContinue
if ($existing) {
    $actions = @($existing.Actions)
    $triggers = @($existing.Triggers)
    if ($existing.TaskName -cne $TaskName -or $existing.TaskPath -cne '\' -or
        $existing.Description -ne $description -or $actions.Count -ne 1 -or
        $actions[0].Execute -ine $powershellExecutable -or $actions[0].Arguments -cne $arguments -or
        $actions[0].WorkingDirectory -ine $ProjectDirectory -or
        $existing.Principal.UserId -notin @($currentSid, $currentIdentity.Name) -or
        $existing.Principal.LogonType -ne 'Interactive' -or $existing.Principal.RunLevel -ne 'Limited' -or
        [Xml.XmlConvert]::ToTimeSpan($existing.Settings.ExecutionTimeLimit) -ne (New-TimeSpan -Minutes 5) -or
        $triggers.Count -ne 1 -or $triggers[0].CimClass.CimClassName -ne 'MSFT_TaskLogonTrigger' -or
        $triggers[0].UserId -notin @($currentSid, $currentIdentity.Name)) {
        throw 'A different or modified task already uses this name; it was not overwritten.'
    }
    Write-Host 'Compatible existing logon task preserved. No registration or startup was performed.'
    return
}
$action = New-ScheduledTaskAction -Execute $powershellExecutable -Argument $arguments -WorkingDirectory $ProjectDirectory
$trigger = New-ScheduledTaskTrigger -AtLogOn -User $currentSid
$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -MultipleInstances IgnoreNew -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 2) -ExecutionTimeLimit (New-TimeSpan -Minutes 5)
$principal = New-ScheduledTaskPrincipal -UserId $currentSid -LogonType Interactive -RunLevel Limited

if ($PSCmdlet.ShouldProcess($TaskName, 'Register fail-closed startup task')) {
    # Do not use -Force: a concurrent or unrelated registration must remain intact.
    Register-ScheduledTask -TaskName $TaskName -TaskPath '\' -Action $action -Trigger $trigger -Settings $settings -Principal $principal -Description $description | Out-Null
    Write-Host 'Registered limited, interactive-user logon startup. The task was not started.'
}
