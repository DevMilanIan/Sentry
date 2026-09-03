[CmdletBinding()]
param()

$ErrorActionPreference = 'Stop'
$os = Get-CimInstance Win32_OperatingSystem
$cpu = Get-CimInstance Win32_Processor
$computer = Get-CimInstance Win32_ComputerSystem
$features = Get-WindowsOptionalFeature -Online -FeatureName Microsoft-Windows-Subsystem-Linux,VirtualMachinePlatform

[pscustomobject]@{
    TimestampUtc = [DateTimeOffset]::UtcNow.ToString('O')
    Windows = $os.Caption
    Version = $os.Version
    Build = $os.BuildNumber
    CPU = $cpu.Name.Trim()
    Cores = $cpu.NumberOfCores
    Threads = $cpu.NumberOfLogicalProcessors
    RAMGiB = [math]::Round($computer.TotalPhysicalMemory / 1GB, 1)
    TimeZone = (Get-TimeZone).Id
} | Format-List

$features | Select-Object FeatureName, State | Format-Table -AutoSize
Get-Service W32Time | Select-Object Status, StartType | Format-Table -AutoSize
w32tm /query /status
w32tm /stripchart /computer:time.windows.com /dataonly /samples:5
nvidia-smi --query-gpu=name,memory.total,driver_version,temperature.gpu --format=csv,noheader
Get-Command git,python,wsl,docker,ollama -ErrorAction SilentlyContinue |
    Select-Object Name, Source | Format-Table -AutoSize

