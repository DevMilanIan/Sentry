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
$existing = Get-ScheduledTask -TaskName $TaskName -TaskPath '\' -ErrorAction SilentlyContinue
if (-not $existing) {
    Write-Host 'No matching startup task exists; nothing was removed.'
    return
}
$ProjectDirectory = (Resolve-Path -LiteralPath $ProjectDirectory).Path
$startupScript = Join-Path $ProjectDirectory 'scripts\windows\Start-LocalStack.ps1'
if (-not (Test-Path -LiteralPath $startupScript -PathType Leaf) -or
    $ProjectDirectory.Contains('"') -or $ProjectDirectory.Contains("`n") -or $ProjectDirectory.Contains("`r")) {
    throw 'The expected project startup script could not be safely resolved.'
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
$actions = @($existing.Actions)
$triggers = @($existing.Triggers)
if ($existing.TaskName -cne $TaskName -or $existing.TaskPath -cne '\' -or
    $existing.Description -ne $description -or $actions.Count -ne 1 -or
    $actions[0].Execute -ine $powershellExecutable -or $actions[0].Arguments -cne $arguments -or
    $actions[0].WorkingDirectory -ine $ProjectDirectory -or
    $existing.Principal.UserId -notin @($currentSid, $currentIdentity.Name) -or
    $existing.Principal.LogonType -ne 'Interactive' -or $existing.Principal.RunLevel -ne 'Limited' -or
    $triggers.Count -ne 1 -or $triggers[0].CimClass.CimClassName -ne 'MSFT_TaskLogonTrigger' -or
    $triggers[0].UserId -notin @($currentSid, $currentIdentity.Name)) {
    throw 'A different or modified task uses this name; no task was removed.'
}
if ($PSCmdlet.ShouldProcess($TaskName, 'Unregister the verified project-owned logon task')) {
    Unregister-ScheduledTask -InputObject $existing -Confirm:$false
    Write-Host 'Removed only the verified Options Sentinel logon task. Application data and services were not removed; rerun Install-StartupTask.ps1 to recreate it.'
}
