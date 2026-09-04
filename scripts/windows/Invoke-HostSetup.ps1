#Requires -RunAsAdministrator
[CmdletBinding()]
param(
    [Parameter(Mandatory)]
    [ValidatePattern('^[a-zA-Z0-9-]{1,64}$')]
    [string]$RunId,
    [ValidateSet('Reliability', 'Platform', 'WslUpdate', 'ClockAccuracy')]
    [string[]]$Components = @('Reliability', 'Platform')
)

# Fixed, auditable setup steps only. No reboot, credential handling, or trading startup.
$ErrorActionPreference = 'Stop'
$taskSetupRoot = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..\..\var\setup'))
New-Item -ItemType Directory -Path $taskSetupRoot -Force | Out-Null
$taskStatusPath = Join-Path $taskSetupRoot "host-$RunId.json"
$taskLogPath = Join-Path $taskSetupRoot "host-$RunId.log"
$taskStatus = [ordered]@{
    started_at = [DateTime]::UtcNow.ToString('o')
    completed_at = $null
    status = 'running'
    reliability = 'pending'
    platform = 'pending'
    wsl_update = 'not_requested'
    clock_accuracy = 'not_requested'
    errors = @()
}

function Save-SetupStatus {
    $taskStatus | ConvertTo-Json -Depth 5 | Set-Content -LiteralPath $taskStatusPath -Encoding UTF8
}

Save-SetupStatus
Start-Transcript -Path $taskLogPath | Out-Null
try {
    $taskSteps = @(
        @{ Name = 'reliability'; Script = 'Configure-Reliability.ps1' },
        @{ Name = 'platform'; Script = 'Enable-Platform.ps1' },
        @{ Name = 'wsl_update'; Script = 'Update-Wsl.ps1'; Component = 'WslUpdate' },
        @{ Name = 'clock_accuracy'; Script = 'Configure-ClockAccuracy.ps1'; Component = 'ClockAccuracy' }
    )
    foreach ($taskStep in $taskSteps) {
        $taskComponent = if ($taskStep.Component) { $taskStep.Component } else { $taskStep.Name }
        if ($Components -notcontains $taskComponent) {
            $taskStatus[$taskStep.Name] = 'not_requested'
            continue
        }
        try {
            & (Join-Path $PSScriptRoot $taskStep.Script)
            $taskStatus[$taskStep.Name] = 'completed'
        } catch {
            $taskStatus[$taskStep.Name] = 'failed'
            $taskStatus.errors += "$($taskStep.Name): $($_.Exception.Message)"
            Write-Warning "$($taskStep.Name) setup failed: $($_.Exception.Message)"
        }
        Save-SetupStatus
    }
} finally {
    $taskStatus.completed_at = [DateTime]::UtcNow.ToString('o')
    $taskStatus.status = if ($taskStatus.errors.Count) { 'failed' } else { 'completed' }
    Save-SetupStatus
    Stop-Transcript | Out-Null
}
if ($taskStatus.errors.Count) { exit 1 }
