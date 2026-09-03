[CmdletBinding()]
param(
    [string]$ProjectDirectory = (Resolve-Path (Join-Path $PSScriptRoot '..\..')).Path
)

$ErrorActionPreference = 'Stop'
$envFile = Join-Path $ProjectDirectory '.env'
$disabledFile = Join-Path $ProjectDirectory 'TRADING_DISABLED'
if (-not (Test-Path -LiteralPath $envFile -PathType Leaf)) {
    throw "Missing ignored environment file: $envFile"
}
if (Test-Path -LiteralPath $disabledFile -PathType Leaf) {
    Write-Warning 'TRADING_DISABLED exists. The application will start fail-closed.'
}
$timeService = Get-Service W32Time
if ($timeService.Status -ne 'Running') {
    throw 'Windows Time service is not running; refusing timestamp-sensitive startup.'
}
if (-not (Get-Command docker -ErrorAction SilentlyContinue)) {
    throw 'Docker CLI is unavailable.'
}

Push-Location $ProjectDirectory
try {
    docker compose up --build --detach
    docker compose ps
} finally {
    Pop-Location
}

