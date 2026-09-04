#Requires -Version 7.0
[CmdletBinding(SupportsShouldProcess)]
param(
    [ValidateRange(1, 120)][int]$TimeoutSeconds = 120,
    [switch]$CheckOnly
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

function Resolve-VerifiedApplication([string[]]$Candidates, [string]$PublisherPattern) {
    foreach ($candidate in $Candidates) {
        if (-not (Test-Path -LiteralPath $candidate -PathType Leaf)) { continue }
        $resolved = (Resolve-Path -LiteralPath $candidate).Path
        $signature = Get-AuthenticodeSignature -LiteralPath $resolved
        if ($signature.Status -ne 'Valid' -or -not $signature.SignerCertificate -or
            $signature.SignerCertificate.Subject -notmatch $PublisherPattern) {
            throw 'An installed dependency has an invalid or unexpected executable signature.'
        }
        return $resolved
    }
    throw 'A required signed local dependency was not found in its expected install locations.'
}

function Test-DockerReady([string]$Executable, [int]$TimeoutMilliseconds) {
    $probe = New-Object Diagnostics.Process
    $probe.StartInfo = New-Object Diagnostics.ProcessStartInfo
    $probe.StartInfo.FileName = $Executable
    $probe.StartInfo.UseShellExecute = $false
    $probe.StartInfo.CreateNoWindow = $true
    $probe.StartInfo.RedirectStandardOutput = $true
    $probe.StartInfo.RedirectStandardError = $true
    foreach ($argument in @('--host', 'npipe:////./pipe/dockerDesktopLinuxEngine',
            'info', '--format', '{{.ServerVersion}}')) {
        $probe.StartInfo.ArgumentList.Add($argument)
    }
    foreach ($key in @('DOCKER_HOST', 'DOCKER_CONTEXT', 'DOCKER_TLS_VERIFY', 'DOCKER_CERT_PATH')) {
        $probe.StartInfo.Environment.Remove($key) | Out-Null
    }
    try {
        if (-not $probe.Start()) { return $false }
        $output = $probe.StandardOutput.ReadToEndAsync()
        $errors = $probe.StandardError.ReadToEndAsync()
        if (-not $probe.WaitForExit($TimeoutMilliseconds)) {
            # Only this exact diagnostic child is terminated; never an engine or app.
            $probe.Kill()
            return $false
        }
        return $probe.ExitCode -eq 0 -and $output.GetAwaiter().GetResult().Trim() -match '^\d+\.\d+'
    } catch {
        return $false
    } finally {
        $probe.Dispose()
    }
}

function Test-OllamaReady([int]$TimeoutMilliseconds) {
    $handler = New-Object Net.Http.HttpClientHandler
    $handler.AllowAutoRedirect = $false
    $handler.UseProxy = $false
    $client = New-Object Net.Http.HttpClient($handler)
    $client.Timeout = [TimeSpan]::FromMilliseconds($TimeoutMilliseconds)
    $client.MaxResponseContentBufferSize = 4096
    try {
        $response = $client.GetAsync('http://127.0.0.1:11434/api/version').GetAwaiter().GetResult()
        try {
            if (-not $response.IsSuccessStatusCode) { return $false }
            $payload = $response.Content.ReadAsStringAsync().GetAwaiter().GetResult() | ConvertFrom-Json
            return $payload.version -match '^\d+\.\d+\.\d+'
        } finally {
            $response.Dispose()
        }
    } catch {
        return $false
    } finally {
        $client.Dispose()
        $handler.Dispose()
    }
}

function Test-ExpectedProcess([string[]]$Names, [string]$InstallDirectory) {
    $root = [IO.Path]::GetFullPath($InstallDirectory).TrimEnd('\') + '\'
    $found = $false
    foreach ($name in $Names) {
        foreach ($process in @(Get-Process -Name $name -ErrorAction SilentlyContinue)) {
            $path = $process.Path
            if (-not $path -or -not $path.StartsWith($root, [StringComparison]::OrdinalIgnoreCase)) {
                throw 'A dependency-named process has an unknown executable path; refusing a duplicate launch.'
            }
            $found = $true
        }
    }
    return $found
}

$dockerDesktop = Resolve-VerifiedApplication @(
    (Join-Path $env:LOCALAPPDATA 'Programs\DockerDesktop\Docker Desktop.exe'),
    (Join-Path $env:ProgramFiles 'Docker\Docker\Docker Desktop.exe')
) '(?:^|,\s*)O=Docker Inc(?:,|$)'
$dockerDirectory = Split-Path -Parent $dockerDesktop
$dockerCli = Resolve-VerifiedApplication @(
    (Join-Path $dockerDirectory 'resources\bin\docker.exe')
) '(?:^|,\s*)O=Docker Inc(?:,|$)'
$ollamaApp = Resolve-VerifiedApplication @(
    (Join-Path $env:LOCALAPPDATA 'Programs\Ollama\ollama app.exe'),
    (Join-Path $env:ProgramFiles 'Ollama\ollama app.exe')
) '(?:^|,\s*)O=Ollama Inc\.(?:,|$)'
$ollamaDirectory = Split-Path -Parent $ollamaApp
# Validate the server binary as well; the app owns starting it when absent.
$null = Resolve-VerifiedApplication @((Join-Path $ollamaDirectory 'ollama.exe')) '(?:^|,\s*)O=Ollama Inc\.(?:,|$)'

$timer = [Diagnostics.Stopwatch]::StartNew()
$dockerReady = $false
$ollamaReady = $false
$launchAttempted = $false
while ($timer.Elapsed.TotalSeconds -lt $TimeoutSeconds) {
    $remaining = [int][Math]::Max(1, ($TimeoutSeconds * 1000) - $timer.ElapsedMilliseconds)
    $dockerReady = Test-DockerReady $dockerCli ([Math]::Min(3000, $remaining))
    $remaining = [int](($TimeoutSeconds * 1000) - $timer.ElapsedMilliseconds)
    if ($remaining -le 0) { break }
    $ollamaReady = Test-OllamaReady ([Math]::Min(2000, $remaining))
    if ($dockerReady -and $ollamaReady) {
        Write-Host 'Verified local Docker Desktop Linux engine and native Ollama are ready.'
        return [pscustomobject]@{ DockerCli = $dockerCli; DockerHost = 'npipe:////./pipe/dockerDesktopLinuxEngine' }
    }
    if ($CheckOnly) { throw 'A local dependency is not ready; CheckOnly did not start any process.' }
    if (-not $launchAttempted) {
        $launchAttempted = $true
        if (-not $dockerReady -and -not (Test-ExpectedProcess @('Docker Desktop') $dockerDirectory)) {
            if ($PSCmdlet.ShouldProcess($dockerDesktop, 'Start installed Docker Desktop hidden')) {
                Start-Process -FilePath $dockerDesktop -WindowStyle Hidden | Out-Null
            } else { return }
        }
        if (-not $ollamaReady -and -not (Test-ExpectedProcess @('ollama app', 'ollama') $ollamaDirectory)) {
            if ($PSCmdlet.ShouldProcess($ollamaApp, 'Start installed Ollama application hidden')) {
                Start-Process -FilePath $ollamaApp -WindowStyle Hidden | Out-Null
            } else { return }
        }
    }
    $remaining = [int](($TimeoutSeconds * 1000) - $timer.ElapsedMilliseconds)
    if ($remaining -gt 0) { Start-Sleep -Milliseconds ([Math]::Min(1000, $remaining)) }
}
throw 'Local dependencies did not become ready within the shared timeout. Check Docker Desktop onboarding or service health manually; no login, agreement, or subscription action was taken.'
