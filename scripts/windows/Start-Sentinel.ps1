[CmdletBinding()]
param(
    [string]$ProjectDirectory = (Resolve-Path (Join-Path $PSScriptRoot '..\..')).Path,
    [string]$EnvironmentFile = $(if ($env:SENTRY_ENV_FILE) { $env:SENTRY_ENV_FILE } else {
        Join-Path $env:USERPROFILE '.options-sentinel\runtime.env'
    }),
    [switch]$Build
)

$ErrorActionPreference = 'Stop'
$envFile = [IO.Path]::GetFullPath($EnvironmentFile)
$disabledFile = Join-Path $ProjectDirectory 'var\TRADING_DISABLED'
if (-not (Test-Path -LiteralPath $envFile -PathType Leaf)) {
    throw 'Missing private environment file. Run Initialize-LocalEnvironment.ps1 first.'
}
& (Join-Path $PSScriptRoot 'Initialize-LocalEnvironment.ps1') -EnvironmentFile $envFile -ValidateOnly
if (Test-Path -LiteralPath $disabledFile -PathType Leaf) {
    Write-Warning 'TRADING_DISABLED exists. The application will start fail-closed.'
}
& (Join-Path $PSScriptRoot 'Test-ClockHealth.ps1')
if (-not (Get-Command docker -ErrorAction SilentlyContinue)) {
    throw 'Docker CLI is unavailable.'
}

Push-Location $ProjectDirectory
# Compose shell variables take precedence over --env-file. Remove inherited overrides
# for this invocation so interpolation and the application's env_file cannot disagree.
$runtimeKeys = @('POSTGRES_PASSWORD', 'SENTRY_CONFIG', 'SENTRY_EXECUTION_ENVIRONMENT',
    'SENTRY_DEMO_BACKEND', 'SENTRY_TRADING_MODE', 'SENTRY_DATABASE_URL', 'SENTRY_OLLAMA_URL',
    'SENTRY_OLLAMA_MODEL', 'SENTRY_DASHBOARD_TOKEN', 'SENTRY_LIVE_AUTHORIZATION_FILE')
$previousEnvironment = @{}
try {
    foreach ($key in $runtimeKeys + @('SENTRY_ENV_FILE')) {
        $previousEnvironment[$key] = @{
            Exists = Test-Path -LiteralPath "Env:$key"
            Value = [Environment]::GetEnvironmentVariable($key, 'Process')
        }
    }
    foreach ($key in $runtimeKeys) {
        # PowerShell 7.6/.NET turns a $null string argument into an empty value.
        # An empty shell variable still overrides Compose's --env-file value.
        if (Test-Path -LiteralPath "Env:$key") { Remove-Item -LiteralPath "Env:$key" }
    }
    $env:SENTRY_ENV_FILE = $envFile
    if ($Build) {
        docker compose --env-file $envFile up --build --detach
    } else {
        # Logon only starts previously deployed images; missing images fail closed.
        docker compose --env-file $envFile up --no-build --detach --pull never
    }
    if ($LASTEXITCODE -ne 0) { throw "Compose startup failed with exit code $LASTEXITCODE." }
    docker compose --env-file $envFile ps
    if ($LASTEXITCODE -ne 0) { throw "Compose status failed with exit code $LASTEXITCODE." }
} finally {
    foreach ($key in $previousEnvironment.Keys) {
        if ($previousEnvironment[$key].Exists) {
            Set-Item -LiteralPath "Env:$key" -Value $previousEnvironment[$key].Value
        } elseif (Test-Path -LiteralPath "Env:$key") {
            Remove-Item -LiteralPath "Env:$key"
        }
    }
    Pop-Location
}
