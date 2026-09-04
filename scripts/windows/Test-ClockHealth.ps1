[CmdletBinding()]
param(
    [ValidateRange(1, 250)][int]$MaximumClockOffsetMilliseconds = 250,
    [ValidateRange(1, 15)][int]$NativeTimeoutSeconds = 12
)

$ErrorActionPreference = 'Stop'

function Invoke-BoundedClockCommand {
    param([string[]]$Arguments, [int]$TimeoutSeconds)
    $info = [Diagnostics.ProcessStartInfo]::new()
    $info.FileName = Join-Path $env:SystemRoot 'System32\w32tm.exe'
    $info.UseShellExecute = $false
    $info.CreateNoWindow = $true
    $info.RedirectStandardOutput = $true
    $info.RedirectStandardError = $true
    foreach ($argument in $Arguments) { $info.ArgumentList.Add($argument) }
    $process = [Diagnostics.Process]::new()
    $process.StartInfo = $info
    try {
        if (-not $process.Start()) { throw 'Could not start the clock measurement.' }
        $stdout = $process.StandardOutput.ReadToEndAsync()
        $stderr = $process.StandardError.ReadToEndAsync()
        if (-not $process.WaitForExit($TimeoutSeconds * 1000)) {
            $process.Kill($true)
            $null = $process.WaitForExit(2000)
            throw 'Clock measurement exceeded its bounded timeout.'
        }
        $output = $stdout.GetAwaiter().GetResult()
        $errorOutput = $stderr.GetAwaiter().GetResult()
        if ($process.ExitCode -ne 0 -or $errorOutput.Trim()) {
            throw 'Windows Time query failed; synchronized clock health is unproven.'
        }
        if ($output.Length -gt 32768) { throw 'Unexpectedly large Windows Time response.' }
        return $output
    } finally {
        $process.Dispose()
    }
}

function ConvertFrom-ClockStatus {
    param([string]$StatusText)
    # w32tm has no JSON status format. Validate the documented verbose field order
    # and numeric values, not translated labels or locale-dependent date strings.
    # A changed/unrecognized format is an error, never synchronization evidence.
    $lines = @($StatusText -split '\r?\n' | Where-Object { $_.Trim() })
    if ($lines.Count -ne 16) { throw 'Unrecognized Windows Time status format.' }
    $values = foreach ($line in $lines) {
        if ($line -notmatch '^[^:]+:\s*(.+)$') {
            throw 'Unrecognized Windows Time status field.'
        }
        $Matches[1].Trim()
    }
    $numbers = @{}
    foreach ($index in @(0, 1, 8, 11, 12, 13, 14)) {
        if ($values[$index] -notmatch '^(\d+)(?:\s|\(|$)') {
            throw 'Unrecognized numeric Windows Time status field.'
        }
        $numbers[$index] = [int]$Matches[1]
    }
    if ($numbers[0] -notin @(0, 1, 2) -or $numbers[1] -lt 1 -or $numbers[1] -gt 15 -or
        $numbers[11] -notin @(1, 2) -or $numbers[14] -ne 0) {
        throw 'Windows Time does not report a synchronized usable source.'
    }
    if ($values[7] -notmatch '^time\.windows\.com(?:,0x[0-9a-f]+)?$') {
        throw 'Windows Time has not selected the configured network reference.'
    }
    if ($values[15] -notmatch '^(\d+(?:[.,]\d+)?)[^\d\s]*$') {
        throw 'Windows Time synchronization age is unrecognized.'
    }
    $age = [double]::Parse($Matches[1].Replace(',', '.'), [Globalization.CultureInfo]::InvariantCulture)
    if ($age -gt 300) { throw 'The most recent Windows Time synchronization is stale.' }
    return [ordered]@{
        source = $values[7]
        leap_indicator = $numbers[0]
        stratum = $numbers[1]
        state = $numbers[11]
        last_sync_error = $numbers[14]
        last_sync_age_seconds = $age
    }
}

function ConvertFrom-ClockSamples {
    param([string]$SamplesText, [int]$MaximumOffsetMilliseconds = 250)
    if ($MaximumOffsetMilliseconds -lt 1 -or $MaximumOffsetMilliseconds -gt 250) {
        throw 'Clock offset gate cannot exceed 250 milliseconds.'
    }
    $offsets = @()
    $delays = @()
    foreach ($line in ($SamplesText -split '\r?\n')) {
        # /rdtsc emits invariant CSV numeric records. Headers need not be English.
        if ($line -match '^\s*\d+\s*,\s*\d+\s*,\s*\d+\s*,\s*([+-]?\d+\.\d+)\s*,\s*([+-]?\d+\.\d+)\s*$') {
            $delay = [double]::Parse($Matches[1], [Globalization.CultureInfo]::InvariantCulture) * 1000
            $offset = [double]::Parse($Matches[2], [Globalization.CultureInfo]::InvariantCulture) * 1000
            if ($delay -lt 0 -or $delay -gt 250 -or [Math]::Abs($offset) -gt $MaximumOffsetMilliseconds) {
                throw 'Clock offset or measurement roundtrip delay exceeds the safety limit.'
            }
            $delays += $delay
            $offsets += $offset
        }
    }
    if ($offsets.Count -ne 5) { throw 'Five valid clock measurement samples are required.' }
    return [ordered]@{ offsets_ms = $offsets; roundtrip_delays_ms = $delays }
}

function Write-ClockHealthEvidence {
    param([System.Collections.IDictionary]$Evidence)
    $projectRoot = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..\..'))
    $healthDirectory = [IO.Path]::GetFullPath((Join-Path $projectRoot 'var\health'))
    if (-not $healthDirectory.StartsWith($projectRoot + [IO.Path]::DirectorySeparatorChar,
            [StringComparison]::OrdinalIgnoreCase)) {
        throw 'Clock evidence path escaped the project.'
    }
    foreach ($path in @((Join-Path $projectRoot 'var'), $healthDirectory,
            (Join-Path $healthDirectory 'windows-clock.json'))) {
        if (Test-Path -LiteralPath $path) {
            if ((Get-Item -LiteralPath $path -Force).Attributes -band [IO.FileAttributes]::ReparsePoint) {
                throw 'Clock evidence paths must not be symbolic links or junctions.'
            }
        }
    }
    $null = New-Item -ItemType Directory -Path $healthDirectory -Force
    $evidencePath = Join-Path $healthDirectory 'windows-clock.json'
    $temporaryPath = Join-Path $healthDirectory ('clock-' + [Guid]::NewGuid().ToString('N') + '.tmp')
    try {
        $Evidence | ConvertTo-Json -Depth 5 | Set-Content -LiteralPath $temporaryPath -Encoding utf8
        Move-Item -LiteralPath $temporaryPath -Destination $evidencePath -Force
    } finally {
        if (Test-Path -LiteralPath $temporaryPath) { Remove-Item -LiteralPath $temporaryPath }
    }
}

# Dot-sourcing is reserved for parser tests; it performs no host queries or writes.
if ($MyInvocation.InvocationName -eq '.') { return }

$evidence = [ordered]@{
    schema_version = 'windows-clock-health-v1'
    checked_at_utc = [DateTime]::UtcNow.ToString('o')
    healthy = $false
    maximum_offset_ms = $MaximumClockOffsetMilliseconds
    reference = 'time.windows.com'
}
try {
    $service = Get-CimInstance -ClassName Win32_Service -Filter "Name='W32Time'" -OperationTimeoutSec 5
    $evidence.service_state = $service.State
    $evidence.service_start_mode = $service.StartMode
    if ($service.State -ne 'Running' -or $service.StartMode -ne 'Auto') {
        throw 'Windows Time must be running with automatic startup.'
    }
    $evidence.timezone_id = (Get-TimeZone).Id
    if ($evidence.timezone_id -ne 'Eastern Standard Time') {
        throw 'The configured Windows timezone is not America/New_York.'
    }
    $status = Invoke-BoundedClockCommand -Arguments @('/query', '/status', '/verbose') -TimeoutSeconds $NativeTimeoutSeconds
    $evidence.synchronization = ConvertFrom-ClockStatus -StatusText $status
    $samples = Invoke-BoundedClockCommand -Arguments @('/stripchart', '/computer:time.windows.com', '/rdtsc', '/samples:5', '/period:1') -TimeoutSeconds $NativeTimeoutSeconds
    $evidence.measurements = ConvertFrom-ClockSamples -SamplesText $samples -MaximumOffsetMilliseconds $MaximumClockOffsetMilliseconds
    $evidence.healthy = $true
    $evidence.completed_at_utc = [DateTime]::UtcNow.ToString('o')
    Write-ClockHealthEvidence -Evidence $evidence
    Write-Host 'Windows clock health passed: synchronized source, Eastern timezone, and five bounded offset samples.'
} catch {
    # Record failure without retaining native output or potentially sensitive errors.
    $evidence.completed_at_utc = [DateTime]::UtcNow.ToString('o')
    $evidence.failure = 'Clock health was not established; startup remains blocked.'
    Write-ClockHealthEvidence -Evidence $evidence
    throw
}
