from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts/windows/Test-ClockHealth.ps1"


def _powershell(command: str) -> subprocess.CompletedProcess[str]:
    executable = shutil.which("pwsh")
    if executable is None:
        pytest.skip("PowerShell 7 is required for Windows clock parser tests")
    quoted = str(SCRIPT).replace("'", "''")
    return subprocess.run(  # noqa: S603 - fixed local parser script, synthetic test input only
        [executable, "-NoProfile", "-Command", f". '{quoted}'; {command}"],
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
    )


def _status(**changes: str) -> str:
    values = [
        "0(no warning)",
        "5 (secondary)",
        "-23",
        "0.03s",
        "0.02s",
        "0xA83DD74A",
        "4/9/2026 04:07:01",
        "time.windows.com,0x9",
        "6 (64s)",
        "-0.00001s",
        "0.015625s",
        "2 (Sync)",
        "0 (None)",
        "0 (None)",
        "0 (Success)",
        "12,4s",
    ]
    for index, value in changes.items():
        values[int(index)] = value
    # These are intentionally not the English labels emitted on the current host.
    return "\n".join(f"Field {index}: {value}" for index, value in enumerate(values))


def _samples(offset: str = "+00.0200000", delay: str = "+00.0300000", count: int = 5) -> str:
    return "Localized header\n" + "\n".join(
        f"1000, 2000, 134329827275579043, {delay}, {offset}" for _ in range(count)
    )


def test_translated_labels_and_decimal_comma_status_are_accepted() -> None:
    result = _powershell(
        "$value = ConvertFrom-ClockStatus -StatusText @'\n"
        + _status()
        + "\n'@; if ($value.last_sync_age_seconds -ne 12.4) { exit 1 }"
    )
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize(
    "changes",
    [
        {"0": "3(no sync)"},
        {"1": "0 (unspecified)"},
        {"1": "16 (invalid)"},
        {"7": "Local CMOS Clock"},
        {"7": "unexpected.example.test"},
        {"11": "0 (UNSET)"},
        {"11": "3 (SPIKE)"},
        {"14": "1 (Error)"},
        {"15": "301.0s"},
    ],
)
def test_unsynchronized_or_stale_status_fails_closed(changes: dict[str, str]) -> None:
    result = _powershell(
        "try { ConvertFrom-ClockStatus -StatusText @'\n"
        + _status(**changes)
        + "\n'@; exit 1 } catch { exit 0 }"
    )
    assert result.returncode == 0, result.stderr


def test_five_invariant_numeric_offset_samples_are_required() -> None:
    result = _powershell(
        "$value = ConvertFrom-ClockSamples -SamplesText @'\n"
        + _samples(offset="-00.2500000")
        + "\n'@; if ($value.offsets_ms.Count -ne 5) { exit 1 }"
    )
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize(
    "sample_text",
    [
        _samples(offset="+00.2500001"),
        _samples(offset="-00.2500001"),
        _samples(delay="+00.2500001"),
        _samples(delay="-00.0010000"),
        _samples(offset="NaN"),
        _samples(count=4),
        _samples(count=6),
    ],
)
def test_failed_or_incomplete_offset_measurements_deny_startup(sample_text: str) -> None:
    result = _powershell(
        "try { ConvertFrom-ClockSamples -SamplesText @'\n"
        + sample_text
        + "\n'@; exit 1 } catch { exit 0 }"
    )
    assert result.returncode == 0, result.stderr


def test_startup_runs_clock_gate_before_compose_and_preserves_private_environment() -> None:
    source = (ROOT / "scripts/windows/Start-Sentinel.ps1").read_text(encoding="utf-8-sig")
    assert source.index("Test-ClockHealth.ps1") < source.index("docker compose")
    assert "Initialize-LocalEnvironment.ps1" in source
    assert "SENTRY_ENV_FILE" in source
    assert "Remove-Item -LiteralPath \"Env:$key\"" in source
    probe = SCRIPT.read_text(encoding="utf-8")
    assert "ValidateRange(1, 250)" in probe
    assert "ValidateRange(1, 15)" in probe
    assert "$process.WaitForExit($TimeoutSeconds * 1000)" in probe
    assert "$process.Kill($true)" in probe
    assert "ReparsePoint" in probe
    assert "Restart-Service" not in probe
    assert "Set-Service" not in probe
    assert "Set-ItemProperty" not in probe


def test_probe_parses_without_powershell_syntax_errors() -> None:
    quoted = str(SCRIPT).replace("'", "''")
    result = _powershell(
        "$tokens = $null; $errors = $null; "
        f"$null = [Management.Automation.Language.Parser]::ParseFile('{quoted}', "
        "[ref]$tokens, [ref]$errors); if ($errors.Count) { exit 1 }"
    )
    assert result.returncode == 0, result.stderr
