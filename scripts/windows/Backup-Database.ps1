#Requires -Version 7.0
[CmdletBinding()]
param()

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

# Backups can contain account identifiers. Never place them in the synced project.
$profileDirectory = [Environment]::GetFolderPath([Environment+SpecialFolder]::UserProfile)
$privateDirectory = Join-Path $profileDirectory '.options-sentinel'
$backupDirectory = Join-Path $privateDirectory 'backups'
$dockerCli = Join-Path $profileDirectory 'AppData\Local\Programs\DockerDesktop\resources\bin\docker.exe'
$dockerHost = 'npipe:////./pipe/dockerDesktopLinuxEngine'
$currentSid = [Security.Principal.WindowsIdentity]::GetCurrent().User.Value
$allowedSids = @($currentSid, 'S-1-5-18', 'S-1-5-32-544')

function Assert-PrivatePath([string]$Path) {
    $ancestor = $Path
    while ($ancestor) {
        if (Test-Path -LiteralPath $ancestor) {
            if ((Get-Item -LiteralPath $ancestor -Force).Attributes -band [IO.FileAttributes]::ReparsePoint) {
                throw 'Backup storage cannot use reparse points.'
            }
        }
        $ancestor = [IO.Path]::GetDirectoryName($ancestor)
    }
    $acl = Get-Acl -LiteralPath $Path
    if (-not $acl.AreAccessRulesProtected -or
        $acl.GetOwner([Security.Principal.SecurityIdentifier]).Value -notin $allowedSids) {
        throw 'Backup storage must already have a private protected ACL.'
    }
    foreach ($rule in $acl.GetAccessRules($true, $true, [Security.Principal.SecurityIdentifier])) {
        if ($rule.AccessControlType -eq [Security.AccessControl.AccessControlType]::Allow -and
            $rule.IdentityReference.Value -notin $allowedSids) {
            throw 'Backup storage permits an unexpected principal.'
        }
    }
}

function Invoke-PrivateDocker([string[]]$Arguments) {
    $startInfo = [Diagnostics.ProcessStartInfo]::new($dockerCli)
    $startInfo.UseShellExecute = $false
    $startInfo.CreateNoWindow = $true
    $startInfo.RedirectStandardOutput = $true
    $startInfo.RedirectStandardError = $true
    foreach ($value in @('--host', $dockerHost) + $Arguments) {
        $startInfo.ArgumentList.Add($value)
    }
    # Ambient Docker configuration must not redirect the selected local endpoint.
    foreach ($key in @('DOCKER_CONTEXT', 'DOCKER_TLS_VERIFY', 'DOCKER_CERT_PATH')) {
        $startInfo.Environment.Remove($key) | Out-Null
    }
    $process = [Diagnostics.Process]::new()
    $process.StartInfo = $startInfo
    try {
        if (-not $process.Start()) { throw 'Backup subprocess did not start.' }
        $output = $process.StandardOutput.ReadToEndAsync()
        $errorOutput = $process.StandardError.ReadToEndAsync()
        if (-not $process.WaitForExit(120000)) {
            $process.Kill($true)
            $process.WaitForExit()
            throw 'Backup subprocess exceeded its deadline.'
        }
        $result = $output.GetAwaiter().GetResult()
        $null = $errorOutput.GetAwaiter().GetResult()
        if ($process.ExitCode -ne 0) { throw 'Local database backup command failed; raw output suppressed.' }
        return $result.Trim()
    } finally { $process.Dispose() }
}

$containerId = $null
$temporaryDump = $null
$verificationDatabase = $null
$verificationMarker = $null
try {
    Assert-PrivatePath $privateDirectory
    if (-not (Test-Path -LiteralPath $backupDirectory)) {
        [IO.Directory]::CreateDirectory($backupDirectory) | Out-Null
        $acl = Get-Acl -LiteralPath $backupDirectory
        $acl.SetAccessRuleProtection($true, $true)
        Set-Acl -LiteralPath $backupDirectory -AclObject $acl
    }
    Assert-PrivatePath $backupDirectory
    $signature = Get-AuthenticodeSignature -LiteralPath $dockerCli
    if ($signature.Status -ne 'Valid' -or $signature.SignerCertificate.Subject -notmatch 'Docker Inc') {
        throw 'The expected signed local Docker CLI is unavailable.'
    }
    $containerId = Invoke-PrivateDocker @('ps', '--filter', 'label=com.docker.compose.project=options-sentinel',
        '--filter', 'label=com.docker.compose.service=postgres', '--format', '{{.ID}}')
    if ($containerId -cnotmatch '^[a-f0-9]{12,64}$') {
        throw 'Exactly one running Options Sentinel PostgreSQL container is required.'
    }
    $databaseVersion = Invoke-PrivateDocker @('exec', $containerId, 'psql', '-X', '-U', 'sentinel',
        '-d', 'sentinel', '-Atqc', 'SHOW server_version_num')
    if ($databaseVersion -cnotmatch '^17[0-9]{4}$') {
        throw 'This backup procedure requires the deployed PostgreSQL 17 database.'
    }
    $backupId = [DateTimeOffset]::UtcNow.ToString('yyyyMMddTHHmmssfffZ') + '-' + [guid]::NewGuid().ToString('N')
    $temporaryDump = '/tmp/options-sentinel-' + $backupId + '.dump'
    $destination = Join-Path $backupDirectory ($backupId + '.dump')
    $manifestPath = Join-Path $backupDirectory ($backupId + '.json')
    if ((Test-Path -LiteralPath $destination) -or (Test-Path -LiteralPath $manifestPath)) {
        throw 'Backup destination already exists; nothing will be overwritten.'
    }
    # PostgreSQL's local socket authenticates inside the existing container. No
    # password, connection URI, env-file contents, or account data enters argv.
    $null = Invoke-PrivateDocker @('exec', $containerId, 'pg_dump', '-U', 'sentinel', '-d', 'sentinel',
        '--format=custom', '--file', $temporaryDump)
    $tableOfContents = Invoke-PrivateDocker @('exec', $containerId, 'pg_restore', '--list', $temporaryDump)
    if ($tableOfContents -notmatch 'TABLE demo shadow_ledger_events' -or
        $tableOfContents -notmatch 'TABLE live order_intents' -or
        $tableOfContents -notmatch 'TABLE shared source_documents') {
        throw 'Backup archive is missing required application tables.'
    }
    $null = Invoke-PrivateDocker @('cp', ($containerId + ':' + $temporaryDump), $destination)
    $fileAcl = Get-Acl -LiteralPath $destination
    $fileAcl.SetAccessRuleProtection($true, $true)
    Set-Acl -LiteralPath $destination -AclObject $fileAcl
    Assert-PrivatePath $destination
    $archive = Get-Item -LiteralPath $destination
    if ($archive.Length -le 0) { throw 'Backup archive is empty.' }
    # Prove this exact archive restores, never against the deployed database.
    # Both names and SQL values come only from this invocation's generated UUID.
    $verificationMarker = [guid]::NewGuid().ToString('N')
    $verificationDatabase = 'sentinel_backup_verify_' + $verificationMarker
    $null = Invoke-PrivateDocker @('exec', $containerId, 'createdb', '-U', 'sentinel',
        '--maintenance-db=postgres', '--template=template0', $verificationDatabase)
    $null = Invoke-PrivateDocker @('exec', $containerId, 'psql', '-X', '-v', 'ON_ERROR_STOP=1',
        '-U', 'sentinel', '-d', 'postgres', '-Atqc',
        ("COMMENT ON DATABASE $verificationDatabase IS 'options-sentinel-backup:$verificationMarker'"))
    $null = Invoke-PrivateDocker @('exec', $containerId, 'pg_restore', '-U', 'sentinel',
        '--exit-on-error', '--single-transaction', '-d', $verificationDatabase, $temporaryDump)
    $restoredTables = Invoke-PrivateDocker @('exec', $containerId, 'psql', '-X', '-v', 'ON_ERROR_STOP=1',
        '-U', 'sentinel', '-d', $verificationDatabase, '-Atqc',
        "SELECT count(*) FROM information_schema.tables WHERE (table_schema, table_name) IN (('demo','shadow_ledger_events'),('live','order_intents'),('shared','source_documents'))")
    if ($restoredTables -cne '3') { throw 'Isolated backup restoration did not preserve required schemas.' }
    $restoredRevision = Invoke-PrivateDocker @('exec', $containerId, 'psql', '-X', '-v', 'ON_ERROR_STOP=1',
        '-U', 'sentinel', '-d', $verificationDatabase, '-Atqc', 'SELECT version_num FROM alembic_version')
    if ($restoredRevision -cnotmatch '^000[0-9]_[a-z_]+$') {
        throw 'Isolated backup restoration has no recognized migration revision.'
    }
    $manifest = [ordered]@{
        version = 'private-postgres-backup-v1'
        created_at = [DateTimeOffset]::UtcNow.ToString('o')
        backup_id = $backupId
        archive_bytes = $archive.Length
        sha256 = (Get-FileHash -LiteralPath $destination -Algorithm SHA256).Hash.ToLowerInvariant()
        server_version_num = $databaseVersion
        format = 'postgresql-custom'
        archive_index_validated = $true
        restored_and_verified = $true
        restored_revision = $restoredRevision
        restore_check = 'isolated transactional restoration and required schema checks'
        scope = 'complete sentinel database: shared, demo, live; excludes external files and OAuth'
    } | ConvertTo-Json
    $manifestStream = [IO.File]::Open($manifestPath, [IO.FileMode]::CreateNew,
        [IO.FileAccess]::Write, [IO.FileShare]::None)
    try {
        $bytes = [Text.UTF8Encoding]::new($false).GetBytes($manifest + "`n")
        $manifestStream.Write($bytes, 0, $bytes.Length)
        $manifestStream.Flush($true)
    } finally { $manifestStream.Dispose() }
    $manifestAcl = Get-Acl -LiteralPath $manifestPath
    $manifestAcl.SetAccessRuleProtection($true, $true)
    Set-Acl -LiteralPath $manifestPath -AclObject $manifestAcl
    Assert-PrivatePath $manifestPath
    Write-Output ([pscustomobject]@{
        status = 'created'; backup_id = $backupId; archive_bytes = $archive.Length
        archive_index_validated = $true; restored_and_verified = $true
    })
} catch {
    # Partial artifacts are retained for inspection, never overwritten or adopted.
    throw 'Private database backup failed. Raw output was suppressed; any partial local artifacts were preserved.'
} finally {
    if ($containerId -and $verificationDatabase -and $verificationMarker) {
        try {
            $ownerMarker = Invoke-PrivateDocker @('exec', $containerId, 'psql', '-X', '-U', 'sentinel',
                '-d', 'postgres', '-Atqc',
                ("SELECT shobj_description(oid,'pg_database') FROM pg_database WHERE datname='$verificationDatabase'"))
            if ($ownerMarker -cne ('options-sentinel-backup:' + $verificationMarker)) {
                throw 'Disposable database ownership could not be verified.'
            }
            # No FORCE: never disconnect another session to make cleanup succeed.
            $null = Invoke-PrivateDocker @('exec', $containerId, 'dropdb', '-U', 'sentinel',
                '--maintenance-db=postgres', $verificationDatabase)
        } catch { Write-Warning 'An isolated backup-verification database was retained for inspection.' }
    }
    if ($containerId -and $temporaryDump) {
        try {
            # Only this invocation's UUID-named temporary container dump is removed.
            $null = Invoke-PrivateDocker @('exec', $containerId, 'rm', '--', $temporaryDump)
        } catch { Write-Warning 'The UUID-named temporary container archive could not be removed.' }
    }
}
