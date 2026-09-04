[CmdletBinding(SupportsShouldProcess)]
param(
    [string]$EnvironmentFile = (Join-Path $env:LOCALAPPDATA 'OptionsSentinel\runtime.env'),
    [switch]$ValidateOnly
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

function Test-WithinDirectory([string]$Candidate, [string]$Directory) {
    $root = [IO.Path]::GetFullPath($Directory).TrimEnd('\', '/')
    return $Candidate.Equals($root, [StringComparison]::OrdinalIgnoreCase) -or
        $Candidate.StartsWith($root + '\', [StringComparison]::OrdinalIgnoreCase)
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
$privateDirectory = [IO.Path]::GetFullPath((Join-Path $env:LOCALAPPDATA 'OptionsSentinel'))
if (-not [IO.Path]::GetDirectoryName($EnvironmentFile).Equals($privateDirectory,
        [StringComparison]::OrdinalIgnoreCase) -or
    -not [IO.Path]::GetFileName($EnvironmentFile).Equals('runtime.env',
        [StringComparison]::OrdinalIgnoreCase)) {
    throw 'Use the dedicated LocalAppData\OptionsSentinel\runtime.env path; arbitrary directory ACL changes are not allowed.'
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
$ancestor = $EnvironmentFile
while ($ancestor) {
    if (Test-Path -LiteralPath $ancestor) {
        $item = Get-Item -LiteralPath $ancestor -Force
        if (($item.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) {
            throw 'Reparse points are not supported for the private environment path.'
        }
    }
    $ancestor = [IO.Path]::GetDirectoryName($ancestor)
}
$currentSid = [Security.Principal.WindowsIdentity]::GetCurrent().User.Value
$allowedSids = @($currentSid, 'S-1-5-18', 'S-1-5-32-544')

if (Test-Path -LiteralPath $EnvironmentFile -PathType Leaf) {
    Assert-PrivateAcl $parentDirectory $allowedSids
    Assert-PrivateAcl $EnvironmentFile $allowedSids
    Assert-SafeEnvironment $EnvironmentFile
    Write-Host 'Existing private DEMO/OFFLINE_SIM/RESEARCH environment validated and preserved.'
    return
}
if ($ValidateOnly) { throw 'The private environment file does not exist; initialize it first.' }
if (-not $PSCmdlet.ShouldProcess($EnvironmentFile, 'Create private demo environment; never overwrite')) {
    return
}

if (-not (Test-Path -LiteralPath $parentDirectory -PathType Container)) {
    New-Item -ItemType Directory -Path $parentDirectory -Force | Out-Null
}
Set-PrivateAcl $parentDirectory $true $allowedSids

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
