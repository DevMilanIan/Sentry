from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
WRAPPER = ROOT / "scripts/windows/Start-LocalStack.ps1"


def _run_fixture(project: Path, dependency: str, stack: str) -> subprocess.CompletedProcess[str]:
    executable = shutil.which("pwsh")
    if executable is None or os.name != "nt":
        pytest.skip("PowerShell 7 on Windows is required for synthetic startup wrapper tests")
    scripts = project / "scripts/windows"
    scripts.mkdir(parents=True)
    wrapper = scripts / "Start-LocalStack.ps1"
    wrapper.write_text(WRAPPER.read_text(encoding="utf-8"), encoding="utf-8")
    (scripts / "Ensure-LocalDependencies.ps1").write_text(dependency, encoding="utf-8")
    (scripts / "Start-Sentinel.ps1").write_text(
        "param([string]$ProjectDirectory, [string]$EnvironmentFile)\n" + stack,
        encoding="utf-8",
    )
    quoted_wrapper = str(wrapper).replace("'", "''")
    quoted_project = str(project).replace("'", "''")
    command = (
        "$env:DOCKER_HOST='synthetic-previous-host'; "
        "$env:DOCKER_CONTEXT='synthetic-previous-context'; "
        "$previousFixturePath=$env:PATH; "
        f"& '{quoted_wrapper}' -ProjectDirectory '{quoted_project}' "
        "-EnvironmentFile 'synthetic-private-env-path'; "
        "$fixtureExit=$LASTEXITCODE; "
        "if ($env:DOCKER_HOST -ne 'synthetic-previous-host' -or "
        "$env:DOCKER_CONTEXT -ne 'synthetic-previous-context' -or "
        "$env:PATH -ne $previousFixturePath) { exit 99 }; exit $fixtureExit"
    )
    return subprocess.run(  # noqa: S603 - copied wrapper invokes synthetic local stubs only
        [executable, "-NoProfile", "-NonInteractive", "-Command", command],
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
    )


def _journal(project: Path) -> list[dict[str, object]]:
    paths = list((project / "var/setup").glob("startup-*.jsonl"))
    assert len(paths) == 1
    rows = [json.loads(line) for line in paths[0].read_text(encoding="utf-8").splitlines()]
    assert len({row["run_id"] for row in rows}) == 1
    assert all(row["evidence_version"] == "local-startup-v1" for row in rows)
    allowed = {
        "evidence_version", "run_id", "recorded_at", "process_id", "stage", "status", "error_class",
        "source_script", "source_line", "private_file_exists", "private_file_matches_user_default",
        "process_userprofile_matches_user_default",
    }
    assert all(set(row) <= allowed for row in rows)
    return rows


def test_startup_journal_records_successful_stages_and_restores_environment(tmp_path: Path) -> None:
    result = _run_fixture(
        tmp_path,
        "[pscustomobject]@{ DockerCli='C:\\synthetic\\docker.exe'; DockerHost='synthetic-host' }",
        "if ($env:DOCKER_HOST -ne 'synthetic-host' -or "
        "(Test-Path -LiteralPath 'Env:DOCKER_CONTEXT')) { throw 'synthetic setup failed' }",
    )
    assert result.returncode == 0, result.stderr
    assert result.stderr == ""
    rows = _journal(tmp_path)
    assert [(row["stage"], row["status"]) for row in rows] == [
        ("initialize", "succeeded"),
        ("dependencies", "started"),
        ("dependencies", "succeeded"),
        ("environment", "started"),
        ("environment", "succeeded"),
        ("stack", "started"),
        ("stack", "succeeded"),
        ("restore", "succeeded"),
        ("complete", "succeeded"),
    ]
    assert "synthetic-private-env-path" not in str(rows)
    assert rows[0]["private_file_exists"] is False
    assert rows[0]["private_file_matches_user_default"] is False


@pytest.mark.parametrize("stage", ["dependencies", "stack"])
def test_startup_journal_identifies_failure_without_exception_values(
    tmp_path: Path, stage: str,
) -> None:
    exception = "throw [InvalidOperationException]::new('private-diagnostic-test-secret')"
    result = _run_fixture(
        tmp_path,
        exception if stage == "dependencies" else (
            "[pscustomobject]@{ DockerCli='C:\\synthetic\\docker.exe'; "
            "DockerHost='synthetic-host' }"
        ),
        exception if stage == "stack" else "throw 'stack must not be called'",
    )
    assert result.returncode == 1
    assert "private-diagnostic-test-secret" not in result.stdout + result.stderr
    console = json.loads(result.stderr)
    assert console["stage"] == stage
    assert console["error_class"] == "System.InvalidOperationException"
    rows = _journal(tmp_path)
    failures = [row for row in rows if row["status"] == "failed"]
    assert len(failures) == 1 and failures[0]["stage"] == stage
    assert failures[0]["source_script"] == (
        "Ensure-LocalDependencies.ps1" if stage == "dependencies" else "Start-Sentinel.ps1"
    )
    assert failures[0]["source_line"] == (1 if stage == "dependencies" else 2)
    assert "private-diagnostic-test-secret" not in str(rows)
    assert not any(row["stage"] == "complete" for row in rows)


def test_startup_journal_directory_failure_does_not_overwrite_existing_file(tmp_path: Path) -> None:
    directory = tmp_path / "var"
    directory.mkdir()
    blocker = directory / "setup"
    blocker.write_text("preserve this unrelated file", encoding="utf-8")
    result = _run_fixture(tmp_path, "throw 'dependency must not be called'", "")
    assert result.returncode == 1
    assert json.loads(result.stderr)["stage"] == "initialize"
    assert blocker.read_text(encoding="utf-8") == "preserve this unrelated file"


def test_startup_journal_is_exclusive_bounded_to_project_and_has_no_raw_error_output() -> None:
    source = WRAPPER.read_text(encoding="utf-8")
    assert "[IO.FileMode]::CreateNew" in source
    assert "[IO.FileShare]::Read" in source
    assert "[IO.FileAttributes]::ReparsePoint" in source
    assert "[IO.Path]::DirectorySeparatorChar" in source
    assert "Write-StartupEvidence $startupJournal $startupId 'complete' 'succeeded'" in source
    assert "$.Exception.Message" not in source
    assert "$_.Exception.Message" not in source
    assert "Write-Error $_" not in source
    assert "$_ |" not in source
    assert "Start-ScheduledTask" not in source
    assert "Remove-Item -LiteralPath $journal" not in source
