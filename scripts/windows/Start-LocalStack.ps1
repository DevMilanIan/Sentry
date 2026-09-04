#Requires -Version 7.0
[CmdletBinding()]
param(
    [string]$ProjectDirectory = (Resolve-Path (Join-Path $PSScriptRoot '..\..')).Path,
    [string]$EnvironmentFile = (Join-Path $env:LOCALAPPDATA 'OptionsSentinel\runtime.env')
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

function Write-StartupEvidence {
    param(
        [IO.StreamWriter]$Writer,
        [string]$RunId,
        [ValidateSet('initialize', 'dependencies', 'environment', 'stack', 'restore', 'complete')]
        [string]$Stage,
        [ValidateSet('started', 'succeeded', 'failed')][string]$Status,
        [string]$ErrorClass = '',
        [string]$SourceScript = '',
        [int]$SourceLine = 0,
        [hashtable]$Facts = @{}
    )
    $entry = [ordered]@{
        evidence_version = 'local-startup-v1'
        run_id = $RunId
        recorded_at = [DateTimeOffset]::UtcNow.ToString('o')
        process_id = $PID
        stage = $Stage
        status = $Status
    }
    if ($ErrorClass) { $entry.error_class = $ErrorClass }
    if ($SourceScript -in @('Start-LocalStack.ps1', 'Start-Sentinel.ps1',
            'Ensure-LocalDependencies.ps1', 'Initialize-LocalEnvironment.ps1', 'Test-ClockHealth.ps1')) {
        $entry.source_script = $SourceScript
        $entry.source_line = $SourceLine
    }
    foreach ($key in @('private_file_exists', 'private_file_matches_user_default',
            'process_local_appdata_matches_user_default')) {
        if ($Facts.ContainsKey($key)) { $entry[$key] = [bool]$Facts[$key] }
    }
    $Writer.WriteLine(($entry | ConvertTo-Json -Compress))
    $Writer.Flush()
}

$startupId = [guid]::NewGuid().ToString('N')
$startupJournal = $null
$startupStage = 'initialize'
$startupFailure = $null
$startupFacts = @{}
$previousPath = $env:PATH
$previousDockerEnvironment = @{}
try {
    # Each invocation owns a new append-only journal. Never overwrite an earlier
    # startup attempt, adopt an existing file, or follow var/setup reparse points.
    $resolvedProject = (Resolve-Path -LiteralPath $ProjectDirectory).Path
    $projectRoot = [IO.Path]::GetFullPath($resolvedProject).TrimEnd('\', '/')
    $setupDirectory = Join-Path $projectRoot 'var\setup'
    foreach ($directory in @((Join-Path $projectRoot 'var'), $setupDirectory)) {
        $absoluteDirectory = [IO.Path]::GetFullPath($directory)
        if (-not $absoluteDirectory.StartsWith($projectRoot + [IO.Path]::DirectorySeparatorChar,
                [StringComparison]::OrdinalIgnoreCase)) {
            throw 'Startup evidence directory is outside the requested project.'
        }
        if (Test-Path -LiteralPath $absoluteDirectory) {
            $item = Get-Item -LiteralPath $absoluteDirectory -Force
            if (-not $item.PSIsContainer -or
                ($item.Attributes -band [IO.FileAttributes]::ReparsePoint)) {
                throw 'Startup evidence directories must be ordinary project directories.'
            }
        } else {
            [IO.Directory]::CreateDirectory($absoluteDirectory) | Out-Null
        }
    }
    $journalName = 'startup-' + [DateTimeOffset]::UtcNow.ToString('yyyyMMddTHHmmssfffZ') + '-' + $startupId + '.jsonl'
    $journalPath = Join-Path $setupDirectory $journalName
    $journalStream = [IO.FileStream]::new($journalPath, [IO.FileMode]::CreateNew,
        [IO.FileAccess]::Write, [IO.FileShare]::Read)
    $startupJournal = [IO.StreamWriter]::new($journalStream, [Text.UTF8Encoding]::new($false))
    $userLocalAppData = [Environment]::GetFolderPath([Environment+SpecialFolder]::LocalApplicationData)
    $expectedPrivateFile = Join-Path $userLocalAppData 'OptionsSentinel\runtime.env'
    $startupFacts = @{
        private_file_exists = Test-Path -LiteralPath $EnvironmentFile -PathType Leaf
        private_file_matches_user_default = [IO.Path]::GetFullPath($EnvironmentFile).Equals(
            [IO.Path]::GetFullPath($expectedPrivateFile), [StringComparison]::OrdinalIgnoreCase)
        process_local_appdata_matches_user_default = [IO.Path]::GetFullPath($env:LOCALAPPDATA).Equals(
            [IO.Path]::GetFullPath($userLocalAppData), [StringComparison]::OrdinalIgnoreCase)
    }
    Write-StartupEvidence $startupJournal $startupId $startupStage 'succeeded' -Facts $startupFacts

    $startupStage = 'dependencies'
    Write-StartupEvidence $startupJournal $startupId $startupStage 'started'
    $dependencies = & (Join-Path $PSScriptRoot 'Ensure-LocalDependencies.ps1') 2>$null
    if (-not $dependencies -or -not $dependencies.DockerCli) {
        throw 'Local dependency readiness was not established.'
    }
    Write-StartupEvidence $startupJournal $startupId $startupStage 'succeeded'

    $startupStage = 'environment'
    Write-StartupEvidence $startupJournal $startupId $startupStage 'started'
    # Scope all subsequent Compose commands to the verified local Linux engine.
    foreach ($key in @('DOCKER_HOST', 'DOCKER_CONTEXT', 'DOCKER_TLS_VERIFY', 'DOCKER_CERT_PATH')) {
        $previousDockerEnvironment[$key] = @{
            Exists = Test-Path -LiteralPath "Env:$key"
            Value = [Environment]::GetEnvironmentVariable($key, 'Process')
        }
        if (Test-Path -LiteralPath "Env:$key") { Remove-Item -LiteralPath "Env:$key" }
    }
    $env:DOCKER_HOST = $dependencies.DockerHost
    $env:PATH = (Split-Path -Parent $dependencies.DockerCli) + ';' + $previousPath
    Write-StartupEvidence $startupJournal $startupId $startupStage 'succeeded'

    $startupStage = 'stack'
    Write-StartupEvidence $startupJournal $startupId $startupStage 'started'
    & (Join-Path $PSScriptRoot 'Start-Sentinel.ps1') -ProjectDirectory $ProjectDirectory -EnvironmentFile $EnvironmentFile 2>$null
    Write-StartupEvidence $startupJournal $startupId $startupStage 'succeeded'
} catch {
    $startupFailure = @{
        Stage = $startupStage
        ErrorClass = $_.Exception.GetType().FullName
        SourceScript = [IO.Path]::GetFileName($_.InvocationInfo.ScriptName)
        SourceLine = $_.InvocationInfo.ScriptLineNumber
    }
    if ($startupJournal) {
        try {
            Write-StartupEvidence $startupJournal $startupId $startupStage 'failed' $startupFailure.ErrorClass `
                -SourceScript $startupFailure.SourceScript -SourceLine $startupFailure.SourceLine -Facts $startupFacts
        } catch { } # Console still receives a fixed, credential-free failure summary below.
    }
} finally {
    try {
        $env:PATH = $previousPath
        foreach ($key in $previousDockerEnvironment.Keys) {
            if ($previousDockerEnvironment[$key].Exists) {
                Set-Item -LiteralPath "Env:$key" -Value $previousDockerEnvironment[$key].Value
            } elseif (Test-Path -LiteralPath "Env:$key") {
                Remove-Item -LiteralPath "Env:$key"
            }
        }
        if ($startupJournal) {
            Write-StartupEvidence $startupJournal $startupId 'restore' 'succeeded'
        }
    } catch {
        if (-not $startupFailure) {
            $startupFailure = @{ Stage = 'restore'; ErrorClass = $_.Exception.GetType().FullName }
        }
    }
    if ($startupJournal) {
        try {
            if (-not $startupFailure) {
                Write-StartupEvidence $startupJournal $startupId 'complete' 'succeeded'
            }
            $startupJournal.Dispose()
        } catch {
            if (-not $startupFailure) {
                $startupFailure = @{ Stage = 'complete'; ErrorClass = $_.Exception.GetType().FullName }
            }
        }
    }
}
if ($startupFailure) {
    # Do not print ErrorRecord, invocation text, native stderr, environment values,
    # exception messages, or inner exceptions: they may contain credentials.
    [Console]::Error.WriteLine((@{
        status = 'failed'; stage = $startupFailure.Stage; error_class = $startupFailure.ErrorClass
        run_id = $startupId
    } | ConvertTo-Json -Compress))
    exit 1
}
Write-Host 'Local startup completed; credential-free stage evidence was recorded.'
exit 0
