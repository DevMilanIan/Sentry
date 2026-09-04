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
