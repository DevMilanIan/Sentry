from __future__ import annotations

import io
import json
import logging
import sys
from pathlib import Path
from typing import Any

import structlog

from app.observability.logging import (
    RedactingJsonFormatter,
    configure_json_logging,
    redact_sensitive,
)


def test_redacts_nested_credentials_without_removing_useful_context() -> None:
    event = {
        "event": "connection failed",
        "request": {"Authorization": "private", "items": [{"refresh_token": "private"}]},
        "error": "postgresql://user:private@localhost/db Bearer abc.secret",
        "account_fingerprint": "private",
        "count": 3,
    }
    result = redact_sensitive(None, "warning", event)
    assert result["request"] == {
        "Authorization": "[REDACTED]",
        "items": [{"refresh_token": "[REDACTED]"}],
    }
    assert "private" not in str(result)
    assert "abc.secret" not in str(result)
    assert result["event"] == "connection failed"
    assert result["count"] == 3


def test_redacts_inline_credentials_and_does_not_render_arbitrary_objects() -> None:
    class PrivateClient:
        def __repr__(self) -> str:
            raise AssertionError("must not render a private object")

    result = redact_sensitive(
        None, "warning",
        {
            "event": (
                'password="private pass" token=private-token '
                "api_key='private key' account-number=private-account "
                "postgresql+asyncpg://private-user:private-password@localhost/db "
                "Authorization: Basic cHJpdmF0ZQ=="
            ),
            "client": PrivateClient(),
            "exception": "Traceback with an unlabelled private credential",
            "Traceback": "Unlabelled private credential in differently cased traceback key",
            "cause": ValueError("another unlabelled private credential"),
        },
    )
    assert "private" not in str(result)
    assert "cHJpdmF0ZQ==" not in str(result)
    assert result["client"] == "[PrivateClient]"
    assert result["cause"] == {"exception_type": "ValueError"}


def test_stdlib_formatter_suppresses_tracebacks_exception_arguments_and_stack_dumps() -> None:
    try:
        raise ValueError("bare-secret-in-exception")
    except ValueError as exc:
        record = logging.LogRecord(
            "safe.logger", logging.ERROR, __file__, 1, "request failed: %s", (exc,), sys.exc_info()
        )
    record.exc_text = "bare-secret-in-cached-traceback"
    record.stack_info = "bare-secret-in-source-line"
    formatted = RedactingJsonFormatter().format(record)
    assert "bare-secret" not in formatted
    payload = json.loads(formatted)
    assert payload["exception_type"] == "ValueError"
    assert payload["logger"] == "safe.logger"
    assert payload["level"] == "error"


def test_stdlib_formatter_failure_cannot_trigger_raw_logging_fallback() -> None:
    cyclic: dict[str, Any] = {"password": "private-password"}
    cyclic["loop"] = cyclic
    record = logging.LogRecord("safe.logger", logging.ERROR, __file__, 1, cyclic, (), None)
    payload = json.loads(RedactingJsonFormatter().format(record))
    assert payload["event"] == "log record suppressed because safe formatting failed"
    assert "private-password" not in str(payload)


def test_configuration_protects_stdlib_structlog_and_existing_console_handlers(
    tmp_path: Path,
) -> None:
    root = logging.getLogger()
    previous_root_handlers, previous_level = list(root.handlers), root.level
    previous_structlog = structlog.get_config()
    console = io.StringIO()
    console_handler = logging.StreamHandler(console)
    owned_logger = logging.getLogger("test_existing_console")
    previous_owned_handlers = list(owned_logger.handlers)
    previous_propagate = owned_logger.propagate
    owned_logger.handlers = [console_handler]
    owned_logger.propagate = False
    prior_formatters = [
        (handler, handler.formatter)
        for logger in logging.Logger.manager.loggerDict.values()
        if isinstance(logger, logging.Logger)
        for handler in logger.handlers
    ]
    path = tmp_path / "safe.jsonl"
    try:
        configure_json_logging(path)
        logging.getLogger("test_stdlib").error(
            "database failed: %s", "postgresql+asyncpg://role:db-secret@localhost/db"
        )
        try:
            raise RuntimeError("unlabelled-exception-secret")
        except RuntimeError:
            owned_logger.exception("request failed")
            structlog.get_logger("test_structlog").error(
                "structured failure", exc_info=True, nested={"refresh_token": "refresh-secret"}
            )
        for handler in root.handlers:
            handler.flush()
        file_output = path.read_text(encoding="utf-8")
        console_output = console.getvalue()
        for secret in ("db-secret", "unlabelled-exception-secret", "refresh-secret"):
            assert secret not in file_output + console_output
        records = [json.loads(line) for line in file_output.splitlines()]
        assert len(records) == 2
        assert records[1]["exception_type"] == "RuntimeError"
        assert json.loads(console_output)["exception_type"] == "RuntimeError"
    finally:
        for handler in root.handlers:
            if handler not in previous_root_handlers:
                handler.close()
        root.handlers = previous_root_handlers
        root.setLevel(previous_level)
        for handler, formatter in prior_formatters:
            handler.setFormatter(formatter)
        owned_logger.handlers = previous_owned_handlers
        owned_logger.propagate = previous_propagate
        console_handler.close()
        structlog.configure(**previous_structlog)
