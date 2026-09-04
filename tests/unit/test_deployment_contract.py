from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml
from alembic import command as migration_command
from alembic.config import Config as AlembicConfig

from app import main
from app.config import load_config


def test_database_upgrade_preserves_percent_encoded_password(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    observed: dict[str, Any] = {}

    def upgrade(config: AlembicConfig, revision: str) -> None:
        observed["url"] = config.get_main_option("sqlalchemy.url")
        observed["revision"] = revision

    monkeypatch.setattr(migration_command, "upgrade", upgrade)
    url = "postgresql+asyncpg://sentinel:test%40password%25value@localhost/sentinel"
    main._upgrade_database(url)
    assert observed == {"url": url, "revision": "head"}


@pytest.mark.parametrize("command", ["database-upgrade", "serve"])
@pytest.mark.parametrize(
    ("section", "field"),
    [
        ("database", "shared_schema"),
        ("database", "demo_schema"),
        ("database", "live_schema"),
        ("demo", "database_schema"),
        ("broker_shadow", "database_schema"),
        ("live", "database_schema"),
    ],
)
def test_production_cli_rejects_unsupported_schemas_before_any_migration(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    command: str,
    section: str,
    field: str,
) -> None:
    loaded = load_config()
    if section == "database":
        loaded = loaded.model_copy(
            update={
                "app": loaded.app.model_copy(
                    update={
                        "database": loaded.app.database.model_copy(update={field: "custom_test"})
                    }
                )
            }
        )
    else:
        profile = getattr(loaded, section)
        loaded = loaded.model_copy(
            update={section: profile.model_copy(update={field: "custom_test"})}
        )
    monkeypatch.setattr(main, "load_config", lambda _path: loaded)

    def unexpected_upgrade(_url: str) -> None:
        pytest.fail("schema validation must happen before attempting database writes")

    monkeypatch.setattr(main, "_upgrade_database", unexpected_upgrade)
    assert main.cli([command]) == 2
    assert "production migrations support only shared/demo/live" in capsys.readouterr().err


def test_compose_verification_is_opt_in_internal_and_excludes_runtime_mounts() -> None:
    compose = yaml.safe_load(Path("docker-compose.yml").read_text(encoding="utf-8"))
    services = compose["services"]
    verification = services["verify"]
    assert verification["profiles"] == ["verification"]
    assert verification["build"]["target"] == "verification"
    assert verification["environment"]["SENTRY_TEST_ALLOW_DATABASE_CREATION"] == "1"
    assert "ports" not in verification
    assert "volumes" not in verification
    assert "env_file" not in verification
    assert "ports" not in services["postgres"]
    assert services["trading-app"]["build"]["target"] == "runtime"
    assert services["trading-app"]["ports"] == ["127.0.0.1:8000:8000"]
    assert "@sha256:" in services["postgres"]["image"]


def test_container_base_and_installed_dependency_resolution_are_pinned() -> None:
    dockerfile = Path("Dockerfile").read_text(encoding="utf-8")
    assert "FROM python:3.12-slim@sha256:" in dockerfile
    assert "--constraint requirements/container-constraints.txt" in dockerfile
    assert "pip install --upgrade pip" not in dockerfile
    constraints = Path("requirements/container-constraints.txt").read_text(encoding="utf-8")
    requirements = [line for line in constraints.splitlines() if line and not line.startswith("#")]
    assert all("==" in requirement for requirement in requirements)
    assert "mcp==2.1.1" in requirements


def test_compose_private_environment_path_and_password_have_no_secret_default() -> None:
    compose = yaml.safe_load(Path("docker-compose.yml").read_text(encoding="utf-8"))
    services = compose["services"]
    assert services["trading-app"]["env_file"] == ["${SENTRY_ENV_FILE:-.env}"]
    for service_name, variable in [
        ("postgres", "POSTGRES_PASSWORD"),
        ("trading-app", "SENTRY_DATABASE_URL"),
        ("verify", "SENTRY_TEST_DATABASE_URL"),
    ]:
        assert "${POSTGRES_PASSWORD:?" in services[service_name]["environment"][variable]


def test_windows_initializer_contract_keeps_secrets_external_private_and_create_new() -> None:
    script = Path("scripts/windows/Initialize-LocalEnvironment.ps1").read_text(encoding="utf-8")
    assert "OptionsSentinel\\runtime.env" in script
    assert "Security.Cryptography.RandomNumberGenerator" in script
    assert "New-Object byte[] 32" in script
    assert "[IO.FileMode]::CreateNew" in script
    assert "[IO.FileMode]::Create," not in script
    assert "$acl.SetAccessRuleProtection($true, $false)" in script
    assert "'S-1-5-18', 'S-1-5-32-544'" in script
    assert script.index("Set-PrivateAcl $parentDirectory $true") < script.index(
        "$random.GetBytes($bytes)"
    )
    assert script.index("if (Test-Path -LiteralPath $EnvironmentFile -PathType Leaf)") < (
        script.index("$random.GetBytes($bytes)")
    )
    assert "SENTRY_EXECUTION_ENVIRONMENT=DEMO" in script
    assert "SENTRY_DEMO_BACKEND=OFFLINE_SIM" in script
    assert "SENTRY_TRADING_MODE=RESEARCH" in script
    assert "'SENTRY_LIVE_AUTHORIZATION_FILE='" in script
    assert "Reparse points are not supported" in script
    assert "arbitrary directory ACL changes are not allowed" in script
    for line in script.splitlines():
        if "Write-Host" in line or "Write-Output" in line:
            assert "$databasePassword" not in line
            assert "$dashboardToken" not in line
            assert "$content" not in line
            assert "$settings" not in line


def test_windows_startup_uses_same_file_for_both_compose_environment_mechanisms() -> None:
    script = Path("scripts/windows/Start-Sentinel.ps1").read_text(encoding="utf-8")
    assert "'Initialize-LocalEnvironment.ps1') -EnvironmentFile $envFile -ValidateOnly" in script
    assert "$env:SENTRY_ENV_FILE = $envFile" in script
    assert "docker compose --env-file $envFile up --build --detach" in script
    assert "[switch]$Build" in script
    assert "if ($Build) {" in script
    assert "docker compose --env-file $envFile up --no-build --detach --pull never" in script
    assert "docker compose --env-file $envFile ps" in script
    assert 'Exists = Test-Path -LiteralPath "Env:$key"' in script
    assert 'Remove-Item -LiteralPath "Env:$key"' in script
    assert 'Set-Item -LiteralPath "Env:$key" -Value $previousEnvironment[$key].Value' in script
    assert "SetEnvironmentVariable($key, $null" not in script
    assert "if ($LASTEXITCODE -ne 0)" in script
    for line in script.splitlines():
        if "Remove-Item" in line:
            assert 'Remove-Item -LiteralPath "Env:$key"' in line
