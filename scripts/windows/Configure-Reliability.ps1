#Requires -RunAsAdministrator
[CmdletBinding(SupportsShouldProcess)]
param(
    [int]$MaximumClockOffsetMilliseconds = 250
)

$ErrorActionPreference = 'Stop'
if ($PSCmdlet.ShouldProcess('Windows Time', 'Set automatic, start, and force synchronization')) {
    Set-Service -Name W32Time -StartupType Automatic
    Start-Service -Name W32Time
    w32tm /config /syncfromflags:manual /manualpeerlist:'time.windows.com,0x9' /update
    if ($LASTEXITCODE -ne 0) { throw "Windows Time configuration failed: $LASTEXITCODE" }
    w32tm /resync /force
    if ($LASTEXITCODE -ne 0) { throw "Windows Time resynchronization failed: $LASTEXITCODE" }
}

if ($PSCmdlet.ShouldProcess('Active AC power plan', 'Disable automatic system sleep')) {
    powercfg /change standby-timeout-ac 0
    if ($LASTEXITCODE -ne 0) { throw "AC sleep configuration failed: $LASTEXITCODE" }
}

$samples = w32tm /stripchart /computer:time.windows.com /dataonly /samples:5
if ($LASTEXITCODE -ne 0) { throw "Clock measurement failed: $LASTEXITCODE" }
$samples | Write-Host
$offsets = foreach ($line in $samples) {
    if ($line -match '([+-]?\d+\.\d+)s$') { [math]::Abs([double]$Matches[1] * 1000) }
}
if (-not $offsets -or ($offsets | Measure-Object -Maximum).Maximum -gt $MaximumClockOffsetMilliseconds) {
    throw "Clock offset still exceeds ${MaximumClockOffsetMilliseconds}ms; trading startup remains blocked."
}

Write-Host 'Reliability baseline passed. Firewall and endpoint protection were not changed.'
