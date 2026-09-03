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
    if ($PSCmdlet.ShouldProcess($package.Label, 'Install or upgrade with winget')) {
        winget install --id $package.Id --exact --silent --accept-package-agreements --accept-source-agreements
    }
}

Write-Host 'Docker Desktop may require an elevated installer/UAC and a sign-out or reboot.'
Write-Host 'Enable Docker Desktop WSL2 backend and Ubuntu integration; do not install a second Docker Engine in Ubuntu.'

