[CmdletBinding(SupportsShouldProcess)]
param()

$ErrorActionPreference = 'Stop'
if (-not (Get-Command winget -ErrorAction SilentlyContinue)) {
    throw 'winget is required. Install/update Microsoft App Installer first.'
}

$packages = @(
    @{ Id = 'Python.Python.3.12'; Label = 'Python 3.12' },
    @{ Id = 'Ollama.Ollama'; Label = 'Ollama' },
    @{ Id = 'Docker.DockerDesktop'; Label = 'Docker Desktop' }
)
foreach ($package in $packages) {
    if ($package.Id -eq 'Docker.DockerDesktop') {
        $taskDockerPaths = @(
            (Join-Path $env:LOCALAPPDATA 'Programs\DockerDesktop\Docker Desktop.exe'),
            (Join-Path $env:ProgramFiles 'Docker\Docker\Docker Desktop.exe')
        )
        if ($taskDockerPaths | Where-Object { Test-Path -LiteralPath $_ -PathType Leaf }) {
            Write-Host 'Docker Desktop already exists; preserve its current installation mode.'
            continue
        }
    }
    winget list --id $package.Id --exact --source winget --accept-source-agreements --disable-interactivity
    if ($LASTEXITCODE -eq 0) {
        Write-Host "$($package.Label) is already installed; no implicit upgrade requested."
        continue
    }
    if ($PSCmdlet.ShouldProcess($package.Label, 'Install or upgrade with winget')) {
        winget install --id $package.Id --exact --source winget --silent --accept-package-agreements --accept-source-agreements --disable-interactivity
        if ($LASTEXITCODE -ne 0) { throw "$($package.Label) install failed: $LASTEXITCODE" }
    }
}

Write-Host 'Docker Desktop may require an elevated installer/UAC and a sign-out or reboot.'
Write-Host 'Enable Docker Desktop WSL2 backend and Ubuntu integration; do not install a second Docker Engine in Ubuntu.'
