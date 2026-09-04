[CmdletBinding(SupportsShouldProcess)]
param(
    [string]$EnvironmentFile = (Join-Path $env:USERPROFILE '.options-sentinel\runtime.env'),
    [switch]$ValidateOnly,
    [string]$MigrateFromEnvironmentFile
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

function Test-WithinDirectory([string]$Candidate, [string]$Directory) {
    $root = [IO.Path]::GetFullPath($Directory).TrimEnd('\', '/')
    return $Candidate.Equals($root, [StringComparison]::OrdinalIgnoreCase) -or
        $Candidate.StartsWith($root + '\', [StringComparison]::OrdinalIgnoreCase)
}

function Assert-NoReparsePoints([string]$Path) {
    $ancestor = $Path
    while ($ancestor) {
        if (Test-Path -LiteralPath $ancestor) {
            $item = Get-Item -LiteralPath $ancestor -Force
            if (($item.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) {
                throw 'Reparse points are not supported for the private environment path.'
            }
        }
        $ancestor = [IO.Path]::GetDirectoryName($ancestor)
    }
}

function Assert-PrivateAcl([string]$Path, [string[]]$AllowedSids) {
    $acl = Get-Acl -LiteralPath $Path
    if (-not $acl.AreAccessRulesProtected) {
        throw 'The local environment ACL must disable inherited permissions.'
    }
    foreach ($rule in $acl.GetAccessRules($true, $true, [Security.Principal.SecurityIdentifier])) {
        if ($rule.AccessControlType -eq [Security.AccessControl.AccessControlType]::Allow -and
            $rule.IdentityReference.Value -notin $AllowedSids) {
            throw 'The local environment ACL permits an unexpected principal.'
        }
    }
    $owner = $acl.GetOwner([Security.Principal.SecurityIdentifier]).Value
    if ($owner -notin $AllowedSids) {
        throw 'The local environment has an unexpected owner.'
    }
}

function Set-PrivateAcl([string]$Path, [bool]$IsDirectory, [string[]]$AllowedSids) {
    if ($IsDirectory) {
        $acl = New-Object Security.AccessControl.DirectorySecurity
        $inheritance = [Security.AccessControl.InheritanceFlags]'ContainerInherit,ObjectInherit'
    } else {
        $acl = New-Object Security.AccessControl.FileSecurity
        $inheritance = [Security.AccessControl.InheritanceFlags]::None
    }
    $acl.SetAccessRuleProtection($true, $false)
    $acl.SetOwner((New-Object Security.Principal.SecurityIdentifier($AllowedSids[0])))
    foreach ($sid in $AllowedSids) {
        $identity = New-Object Security.Principal.SecurityIdentifier($sid)
        $rule = New-Object Security.AccessControl.FileSystemAccessRule(
            $identity, [Security.AccessControl.FileSystemRights]::FullControl, $inheritance,
            [Security.AccessControl.PropagationFlags]::None,
            [Security.AccessControl.AccessControlType]::Allow)
        $acl.AddAccessRule($rule)
    }
    Set-Acl -LiteralPath $Path -AclObject $acl
    Assert-PrivateAcl $Path $AllowedSids
}

function Assert-SafeEnvironment([string]$Path) {
    $settings = @{}
    foreach ($line in [IO.File]::ReadAllLines($Path)) {
        if ([string]::IsNullOrWhiteSpace($line) -or $line.StartsWith('#')) { continue }
        if ($line -notmatch '^([A-Z][A-Z0-9_]*)=(.*)$') {
            throw 'The local environment contains an unsupported line; no values were logged.'
        }
        $key = $Matches[1]
        if ($settings.ContainsKey($key)) { throw 'The local environment contains a duplicate key.' }
        $settings[$key] = $Matches[2]
    }
    $expected = @{
        SENTRY_CONFIG = 'config/app.yaml'
        SENTRY_EXECUTION_ENVIRONMENT = 'DEMO'
        SENTRY_DEMO_BACKEND = 'OFFLINE_SIM'
        SENTRY_TRADING_MODE = 'RESEARCH'
        SENTRY_OLLAMA_URL = 'http://host.docker.internal:11434'
        SENTRY_OLLAMA_MODEL = 'qwen3.5:4b'
        SENTRY_LIVE_AUTHORIZATION_FILE = ''
    }
    $keys = @($expected.Keys) + @('POSTGRES_PASSWORD', 'SENTRY_DASHBOARD_TOKEN', 'SENTRY_DATABASE_URL')
    if ($settings.Count -ne $keys.Count -or @($settings.Keys | Where-Object { $_ -notin $keys }).Count) {
        throw 'The local environment must contain only the supported deployment keys.'
    }
    foreach ($key in $expected.Keys) {
        if (-not $settings.ContainsKey($key) -or $settings[$key] -cne $expected[$key]) {
            throw "The local environment has an unsafe or unsupported setting: $key."
        }
    }
    foreach ($key in @('POSTGRES_PASSWORD', 'SENTRY_DASHBOARD_TOKEN')) {
        if (-not $settings.ContainsKey($key) -or $settings[$key] -cnotmatch '^[a-f0-9]{64}$') {
            throw "The local environment requires a generated 256-bit hex value for $key."
        }
    }
    if ($settings['POSTGRES_PASSWORD'] -ceq $settings['SENTRY_DASHBOARD_TOKEN']) {
        throw 'Database and dashboard credentials must be independent.'
    }
    $expectedUrl = 'postgresql+asyncpg://sentinel:' + $settings['POSTGRES_PASSWORD'] + '@postgres:5432/sentinel'
    if ($settings['SENTRY_DATABASE_URL'] -cne $expectedUrl) {
        throw 'The local database URL does not match the generated private Compose database.'
    }
}

if (-not [IO.Path]::IsPathRooted($EnvironmentFile) -or $EnvironmentFile.StartsWith('\\')) {
    throw 'The environment file must use an absolute local Windows path.'
}
$EnvironmentFile = [IO.Path]::GetFullPath($EnvironmentFile)
$userProfileDirectory = [Environment]::GetFolderPath([Environment+SpecialFolder]::UserProfile)
# AppData may be virtualized differently inside Codex's MSIX package and Task Scheduler.
# This fixed profile directory is shared by both execution contexts.
$privateDirectory = [IO.Path]::GetFullPath((Join-Path $userProfileDirectory '.options-sentinel'))
if (-not [IO.Path]::GetDirectoryName($EnvironmentFile).Equals($privateDirectory,
        [StringComparison]::OrdinalIgnoreCase) -or
    -not [IO.Path]::GetFileName($EnvironmentFile).Equals('runtime.env',
        [StringComparison]::OrdinalIgnoreCase)) {
    throw 'Use the dedicated UserProfile\.options-sentinel\runtime.env path; arbitrary directory ACL changes are not allowed.'
}
$projectDirectory = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot '..\..')).Path
$forbiddenDirectories = @($projectDirectory)
foreach ($name in @('OneDrive', 'OneDriveConsumer', 'OneDriveCommercial')) {
    $oneDriveDirectory = [Environment]::GetEnvironmentVariable($name)
    if ($oneDriveDirectory) { $forbiddenDirectories += $oneDriveDirectory }
}
# Also reject conventional OneDrive directories even when their environment variables are absent.
if ($EnvironmentFile -match '(?i)(?:^|[\\/])OneDrive(?: - [^\\/]+)?(?:[\\/]|$)') {
    throw 'The environment file must remain outside OneDrive.'
}
foreach ($directory in $forbiddenDirectories) {
    if (Test-WithinDirectory $EnvironmentFile $directory) {
        throw 'The environment file must remain outside the repository and OneDrive.'
    }
}
$parentDirectory = [IO.Path]::GetDirectoryName($EnvironmentFile)
Assert-NoReparsePoints $EnvironmentFile
$currentSid = [Security.Principal.WindowsIdentity]::GetCurrent().User.Value
$allowedSids = @($currentSid, 'S-1-5-18', 'S-1-5-32-544')

if ($ValidateOnly -and $MigrateFromEnvironmentFile) {
    throw 'Validation and migration are separate operations.'
}
$legacyRelativePaths = @(
    'AppData\Local\OptionsSentinel\runtime.env',
    'AppData\Local\Packages\OpenAI.Codex_2p2nqsd0c76g0\LocalCache\Local\OptionsSentinel\runtime.env'
)
$legacyPaths = @($legacyRelativePaths | ForEach-Object {
    [IO.Path]::GetFullPath((Join-Path $userProfileDirectory $_))
})
if ($MigrateFromEnvironmentFile) {
    if (-not [IO.Path]::IsPathRooted($MigrateFromEnvironmentFile) -or
        $MigrateFromEnvironmentFile.StartsWith('\\')) {
        throw 'Migration requires an absolute, known local legacy environment path.'
    }
    $MigrateFromEnvironmentFile = [IO.Path]::GetFullPath($MigrateFromEnvironmentFile)
    if ($MigrateFromEnvironmentFile -notin $legacyPaths) {
        throw 'Migration accepts only the known user-local or Codex-private legacy path.'
    }
    Assert-NoReparsePoints $MigrateFromEnvironmentFile
    if (-not (Test-Path -LiteralPath $MigrateFromEnvironmentFile -PathType Leaf)) {
        throw 'The selected legacy environment does not exist; no replacement credentials were generated.'
    }
    Assert-PrivateAcl ([IO.Path]::GetDirectoryName($MigrateFromEnvironmentFile)) $allowedSids
    Assert-PrivateAcl $MigrateFromEnvironmentFile $allowedSids
    Assert-SafeEnvironment $MigrateFromEnvironmentFile
}

if (Test-Path -LiteralPath $EnvironmentFile -PathType Leaf) {
    if ($MigrateFromEnvironmentFile) {
        throw 'Migration refuses an existing destination; existing credentials were not overwritten.'
    }
    Assert-PrivateAcl $parentDirectory $allowedSids
    Assert-PrivateAcl $EnvironmentFile $allowedSids
    Assert-SafeEnvironment $EnvironmentFile
    Write-Host 'Existing private DEMO/OFFLINE_SIM/RESEARCH environment validated and preserved.'
    return
}
if ($ValidateOnly) { throw 'The private environment file does not exist; initialize it first.' }
if (-not $MigrateFromEnvironmentFile -and
    @($legacyPaths | Where-Object { Test-Path -LiteralPath $_ -PathType Leaf }).Count) {
    throw 'A legacy environment exists. Use explicit migration to preserve its credentials; no replacement credentials were generated.'
}
if (-not $PSCmdlet.ShouldProcess($EnvironmentFile, 'Create private demo environment; never overwrite')) {
    return
}

if (-not (Test-Path -LiteralPath $parentDirectory -PathType Container)) {
    New-Item -ItemType Directory -Path $parentDirectory -Force | Out-Null
    Set-PrivateAcl $parentDirectory $true $allowedSids
} else {
    # Do not repair or take ownership of an existing directory silently.
    Assert-PrivateAcl $parentDirectory $allowedSids
}

if ($MigrateFromEnvironmentFile) {
    $sourceStream = $null
    $destinationStream = $null
    try {
        # Copy exact bytes under an exclusive source read lock. Never regenerate,
        # rewrite, delete, or alter the legacy credential file.
        $sourceStream = [IO.File]::Open($MigrateFromEnvironmentFile, [IO.FileMode]::Open,
            [IO.FileAccess]::Read, [IO.FileShare]::Read)
        $destinationStream = [IO.File]::Open($EnvironmentFile, [IO.FileMode]::CreateNew,
            [IO.FileAccess]::Write, [IO.FileShare]::None)
        $sourceStream.CopyTo($destinationStream)
        $destinationStream.Flush($true)
    } catch {
        throw 'Private environment migration failed. Existing files were not overwritten; inspect permissions and completeness without printing contents.'
    } finally {
        if ($destinationStream) { $destinationStream.Dispose() }
        if ($sourceStream) { $sourceStream.Dispose() }
    }
    Set-PrivateAcl $EnvironmentFile $false $allowedSids
    Assert-SafeEnvironment $EnvironmentFile
    Write-Host 'Copied existing private demo credentials exactly; the legacy file was preserved. No credential values were displayed.'
    return
}

$random = [Security.Cryptography.RandomNumberGenerator]::Create()
$bytes = New-Object byte[] 32
$stream = $null
try {
    $random.GetBytes($bytes)
    $databasePassword = ([BitConverter]::ToString($bytes)).Replace('-', '').ToLowerInvariant()
    $random.GetBytes($bytes)
    $dashboardToken = ([BitConverter]::ToString($bytes)).Replace('-', '').ToLowerInvariant()
    $content = @(
        '# Private local runtime configuration. Never copy into the repository or logs.'
        'SENTRY_CONFIG=config/app.yaml'
        'SENTRY_EXECUTION_ENVIRONMENT=DEMO'
        'SENTRY_DEMO_BACKEND=OFFLINE_SIM'
        'SENTRY_TRADING_MODE=RESEARCH'
        "POSTGRES_PASSWORD=$databasePassword"
        "SENTRY_DATABASE_URL=postgresql+asyncpg://sentinel:${databasePassword}@postgres:5432/sentinel"
        'SENTRY_OLLAMA_URL=http://host.docker.internal:11434'
        'SENTRY_OLLAMA_MODEL=qwen3.5:4b'
        "SENTRY_DASHBOARD_TOKEN=$dashboardToken"
        'SENTRY_LIVE_AUTHORIZATION_FILE='
    ) -join "`n"
    # CreateNew refuses existing files, including a concurrent initializer's file.
    # The parent ACL is already private before any credential bytes are written.
    $stream = [IO.File]::Open($EnvironmentFile, [IO.FileMode]::CreateNew,
        [IO.FileAccess]::Write, [IO.FileShare]::None)
    $encoded = (New-Object Text.UTF8Encoding($false)).GetBytes($content + "`n")
    $stream.Write($encoded, 0, $encoded.Length)
    $stream.Flush($true)
} catch {
    # Do not include exception text: runtime failures may contain sensitive input.
    throw 'Private environment creation failed. Existing files were not overwritten; inspect permissions and file completeness without printing contents.'
} finally {
    if ($stream) { $stream.Dispose() }
    $random.Dispose()
    [Array]::Clear($bytes, 0, $bytes.Length)
    if (Get-Variable encoded -ErrorAction SilentlyContinue) { [Array]::Clear($encoded, 0, $encoded.Length) }
    $databasePassword = $null
    $dashboardToken = $null
    $content = $null
}
Set-PrivateAcl $EnvironmentFile $false $allowedSids
Assert-SafeEnvironment $EnvironmentFile
Write-Host 'Created private DEMO/OFFLINE_SIM/RESEARCH environment. LIVE authorization is blank. No credential values were displayed.'
