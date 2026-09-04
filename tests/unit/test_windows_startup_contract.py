from pathlib import Path


def script(name: str) -> str:
    return Path(f"scripts/windows/{name}.ps1").read_text(encoding="utf-8")


def test_dependency_resolution_uses_expected_installs_and_valid_publishers() -> None:
    source = script("Ensure-LocalDependencies")
    assert "Programs\\DockerDesktop\\Docker Desktop.exe" in source
    assert "Docker\\Docker\\Docker Desktop.exe" in source
    assert "Programs\\Ollama\\ollama app.exe" in source
    assert "Get-AuthenticodeSignature -LiteralPath" in source
    assert "$signature.Status -ne 'Valid'" in source
    assert "O=Docker Inc" in source
    assert "O=Ollama Inc" in source
    assert "Get-Command" not in source


def test_dependencies_only_launch_gui_apps_hidden_and_without_duplicate_servers() -> None:
    source = script("Ensure-LocalDependencies")
    launches = [line.strip() for line in source.splitlines() if "Start-Process " in line]
    assert launches == [
        "Start-Process -FilePath $dockerDesktop -WindowStyle Hidden | Out-Null",
        "Start-Process -FilePath $ollamaApp -WindowStyle Hidden | Out-Null",
    ]
    assert "Test-ExpectedProcess @('Docker Desktop')" in source
    assert "Test-ExpectedProcess @('ollama app', 'ollama')" in source
    assert "$launchAttempted = $true" in source
    assert "if ($CheckOnly) { throw" in source
    assert "ollama serve" not in source
    assert "dockerd" not in source
    assert "--accept-license" not in source


def test_readiness_probes_have_one_deadline_and_only_local_endpoints() -> None:
    source = script("Ensure-LocalDependencies")
    assert "[ValidateRange(1, 120)][int]$TimeoutSeconds = 120" in source
    assert source.count("[Diagnostics.Stopwatch]::StartNew()") == 1
    assert "$probe.WaitForExit($TimeoutMilliseconds)" in source
    assert "$probe.Kill()" in source
    assert "npipe:////./pipe/dockerDesktopLinuxEngine" in source
    assert "http://127.0.0.1:11434/api/version" in source
    assert "$handler.AllowAutoRedirect = $false" in source
    assert "$handler.UseProxy = $false" in source
    assert "$client.MaxResponseContentBufferSize = 4096" in source
    assert "$client.Timeout = [TimeSpan]::FromMilliseconds($TimeoutMilliseconds)" in source
    assert "Start-Sleep -Milliseconds ([Math]::Min(1000, $remaining))" in source
    assert "Stop-Process" not in source


def test_logon_wrapper_waits_before_stack_and_restores_process_environment() -> None:
    source = script("Start-LocalStack")
    assert source.index("'Ensure-LocalDependencies.ps1'") < source.index("'Start-Sentinel.ps1'")
    assert "$env:DOCKER_HOST = $dependencies.DockerHost" in source
    assert "$env:PATH = (Split-Path -Parent $dependencies.DockerCli)" in source
    assert 'Exists = Test-Path -LiteralPath "Env:$key"' in source
    assert 'Remove-Item -LiteralPath "Env:$key"' in source
    assert "SetEnvironmentVariable($key, $null" not in source
    assert "$env:PATH = $previousPath" in source
    assert "-Build" not in source


def test_logon_registration_is_signed_pinned_limited_and_never_forces_replacement() -> None:
    source = script("Install-StartupTask")
    assert "Start-LocalStack.ps1" in source
    assert "$signature.Status -eq 'Valid'" in source
    assert "O=Microsoft Corporation" in source
    assert "New-ScheduledTaskAction -Execute $powershellExecutable" in source
    assert "-WindowStyle Hidden -ExecutionPolicy RemoteSigned" in source
    assert "-AtLogOn -User $currentSid" in source
    assert "-LogonType Interactive -RunLevel Limited" in source
    assert "-ExecutionTimeLimit (New-TimeSpan -Minutes 5)" in source
    assert "$existing.Description -ne $description" in source
    assert "$actions[0].Arguments -cne $arguments" in source
    assert "$existing.Principal.LogonType -ne 'Interactive'" in source
    registration = next(
        line for line in source.splitlines() if line.strip().startswith("Register-ScheduledTask ")
    )
    assert "-Force" not in registration
    assert "Start-ScheduledTask" not in source


def test_startup_removal_requires_same_exact_project_action_and_user_identity() -> None:
    source = script("Remove-StartupTask")
    assert "Start-LocalStack.ps1" in source
    assert "$existing.TaskName -cne $TaskName" in source
    assert "$existing.TaskPath -cne '\\'" in source
    assert "$actions[0].Execute -ine $powershellExecutable" in source
    assert "$actions[0].Arguments -cne $arguments" in source
    assert "$actions[0].WorkingDirectory -ine $ProjectDirectory" in source
    assert "$existing.Principal.UserId -notin @($currentSid, $currentIdentity.Name)" in source
    assert "$existing.Principal.LogonType -ne 'Interactive'" in source
    assert "$existing.Principal.RunLevel -ne 'Limited'" in source
    assert "$signature.Status -eq 'Valid'" in source
    assert "O=Microsoft Corporation" in source
    assert "$existing.Description -ne $description" in source
    assert "$triggers[0].CimClass.CimClassName -ne 'MSFT_TaskLogonTrigger'" in source
    assert source.index("no task was removed") < source.index("Unregister-ScheduledTask ")
    assert "Unregister-ScheduledTask -InputObject $existing -Confirm:$false" in source
    assert "Stop-Process" not in source
    assert "Remove-Item" not in source
    assert "ShouldProcess" in source


def test_startup_task_scripts_reject_wildcards_before_querying_task_scheduler() -> None:
    for name in ("Install-StartupTask", "Remove-StartupTask"):
        source = script(name)
        assert source.index("$TaskName.IndexOfAny") < source.index("Get-ScheduledTask ")
        assert "Get-ScheduledTask -TaskName $TaskName -TaskPath '\\'" in source
