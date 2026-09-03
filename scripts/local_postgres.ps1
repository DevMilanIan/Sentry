[CmdletBinding()]
param(
    [ValidateSet('Extract', 'InstallerHelp', 'Status')]
    [string]$Action = 'Status'
)

# Download/signature audit only. The signed EDB installer requires elevation on
# this host; no cluster provisioning or unsigned binary execution is performed.
# Official archive catalog: https://www.enterprisedb.com/download-postgresql-binaries
$ErrorActionPreference = 'Stop'
$pgWorkspace = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..'))
$pgTools = Join-Path $pgWorkspace 'var/tools'
$pgInstall = Join-Path $pgTools 'postgresql-17.11'
$pgArchive = Join-Path $pgTools 'postgresql-17.11-3-windows-x64-binaries.zip'
$pgInstaller = Join-Path $pgTools 'postgresql-17.11-3-windows-x64.exe'
$pgBin = Join-Path $pgInstall 'pgsql/bin'
$pgExpectedHash = '4B8DB0930C38F6EF845DB919551DEDDA3B6B845AEB0927B3D79A6E8E9E4537CF'

function Invoke-HiddenLocalProcess {
    param([string]$Executable, [string[]]$Arguments)
    $pgProcess = [Diagnostics.Process]::new()
    $pgProcess.StartInfo.FileName = $Executable
    $pgProcess.StartInfo.WorkingDirectory = $pgWorkspace
    $pgProcess.StartInfo.UseShellExecute = $false
    $pgProcess.StartInfo.CreateNoWindow = $true
    $pgProcess.StartInfo.RedirectStandardOutput = $true
    $pgProcess.StartInfo.RedirectStandardError = $true
    foreach ($pgArgument in $Arguments) {
        $pgProcess.StartInfo.ArgumentList.Add($pgArgument)
    }
    try {
        if (-not $pgProcess.Start()) { throw 'Could not start local PostgreSQL helper.' }
        $pgOutputTask = $pgProcess.StandardOutput.ReadToEndAsync()
        $pgErrorTask = $pgProcess.StandardError.ReadToEndAsync()
        $pgProcess.WaitForExit()
        $pgOutputTask.GetAwaiter().GetResult()
        $pgErrors = $pgErrorTask.GetAwaiter().GetResult()
        if ($pgErrors) { Write-Output $pgErrors }
        if ($pgProcess.ExitCode -ne 0) {
            throw "PostgreSQL helper exited with code $($pgProcess.ExitCode)."
        }
    }
    finally {
        $pgProcess.Dispose()
    }
}

if ($Action -eq 'InstallerHelp') {
    $pgInstallerSignature = Get-AuthenticodeSignature -LiteralPath $pgInstaller
    if ($pgInstallerSignature.Status -ne 'Valid' -or $pgInstallerSignature.SignerCertificate.Subject -notmatch '^CN=EnterpriseDB Corporation,') {
        throw 'Official EDB installer Authenticode verification failed.'
    }
    Invoke-HiddenLocalProcess -Executable $pgInstaller -Arguments @('--help')
    return
}

if ($Action -eq 'Extract') {
    if ((Get-FileHash -LiteralPath $pgArchive -Algorithm SHA256).Hash -ne $pgExpectedHash) {
        throw 'PostgreSQL archive does not match the official HTTPS download examined for this task.'
    }
    Add-Type -AssemblyName System.IO.Compression.FileSystem
    $pgZip = [IO.Compression.ZipFile]::OpenRead($pgArchive)
    try {
        foreach ($pgEntry in $pgZip.Entries) {
            if ($pgEntry.FullName -notmatch '^pgsql/(bin/|lib/|share/|server_license\.txt$|commandlinetools_3rd_party_licenses\.txt$)') {
                continue
            }
            $pgTarget = [IO.Path]::GetFullPath((Join-Path $pgInstall $pgEntry.FullName))
            if (-not $pgTarget.StartsWith($pgInstall + [IO.Path]::DirectorySeparatorChar, [StringComparison]::OrdinalIgnoreCase)) {
                throw 'Archive path escapes the designated local PostgreSQL directory.'
            }
            if ($pgEntry.FullName.EndsWith('/')) {
                [IO.Directory]::CreateDirectory($pgTarget) | Out-Null
                continue
            }
            [IO.Directory]::CreateDirectory([IO.Path]::GetDirectoryName($pgTarget)) | Out-Null
            if (-not [IO.File]::Exists($pgTarget)) {
                [IO.Compression.ZipFileExtensions]::ExtractToFile($pgEntry, $pgTarget)
            }
        }
    }
    finally {
        $pgZip.Dispose()
    }
    foreach ($pgExecutable in @('postgres.exe', 'initdb.exe', 'pg_ctl.exe', 'psql.exe')) {
        $pgSignature = Get-AuthenticodeSignature -LiteralPath (Join-Path $pgBin $pgExecutable)
        [pscustomobject]@{
            Executable = $pgExecutable
            SignatureStatus = $pgSignature.Status
            Signer = $pgSignature.SignerCertificate.Subject
        }
    }
    return
}

Write-Output 'No local PostgreSQL cluster was provisioned: official archive executables are unsigned and the signed EDB installer requires elevation.'
