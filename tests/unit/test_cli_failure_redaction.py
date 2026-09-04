from __future__ import annotations

import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from app import main
from app.config import DashboardConfig, LoadedConfig
from app.exceptions import ConfigurationError


@pytest.mark.parametrize("configuration_error", [True, False])
def test_cli_failure_does_not_print_exception_values_or_tracebacks(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
    configuration_error: bool,
) -> None:
    def fail_load(_path: Path) -> LoadedConfig:
        exception = ConfigurationError if configuration_error else RuntimeError
        raise exception("bare-secret and postgresql://user:private-password@localhost/database")

    monkeypatch.setattr(main, "load_config", fail_load)
    assert main.cli(["validate-config"]) == (2 if configuration_error else 1)
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "bare-secret" not in captured.err
    assert "private-password" not in captured.err
    assert "Traceback" not in captured.err
    payload = json.loads(captured.err)
    assert payload["error"] == ("configuration" if configuration_error else "RuntimeError")


def test_cli_validation_reports_static_field_names_without_input_or_dynamic_keys(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    def fail_load(_path: Path) -> LoadedConfig:
        try:
            DashboardConfig.model_validate(
                {"port": "private-input-value", "private-extra-key": "private-extra-value"}
            )
        except ValidationError as exc:
            raise ConfigurationError(f"configuration validation failed: {exc}") from exc
        raise AssertionError("invalid model should have failed")

    monkeypatch.setattr(main, "load_config", fail_load)
    assert main.cli(["validate-config"]) == 2
    captured = capsys.readouterr()
    payload = json.loads(captured.err)
    assert "2 issues" in payload["detail"]
    assert "port" in payload["detail"]
    assert "[item]" in payload["detail"]
    assert "private" not in captured.err
    assert "input_value" not in captured.err


def test_cli_does_not_invoke_custom_configuration_exception_stringification(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    class PrivateConfigurationError(ConfigurationError):
        def __str__(self) -> str:
            raise AssertionError("private-stringification-contents")

    def fail_load(_path: Path) -> LoadedConfig:
        raise PrivateConfigurationError("private-configuration-input")

    monkeypatch.setattr(main, "load_config", fail_load)
    assert main.cli(["validate-config"]) == 2
    captured = capsys.readouterr()
    assert "private-" not in captured.err
    assert json.loads(captured.err)["exception_type"] == "PrivateConfigurationError"
