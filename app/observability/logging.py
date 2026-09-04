from __future__ import annotations

import json
import logging
import re
import sys
from collections.abc import Mapping
from datetime import UTC, datetime
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any

import structlog
from structlog.typing import EventDict

_SENSITIVE_KEY = re.compile(
    r"(token|secret|password|authorization|account[_-]?(?:number|fingerprint)|refresh|api[_-]?key)",
    re.I,
)
_URL_CREDENTIAL = re.compile(r"([a-z][a-z0-9+.-]*://)[^\s/@]+@", re.I)
_AUTH_CREDENTIAL = re.compile(r"\b(Bearer|Basic)\s+[A-Za-z0-9._~+/=-]+", re.I)
_KEY_VALUE_CREDENTIAL = re.compile(
    r"(?i)(\b(?:[a-z0-9_-]*(?:token|secret|password)|authorization|"
    r"account[_-]?(?:number|fingerprint)|api[_-]?key)\b[\"']?\s*[:=]\s*)"
    r"(?:\"(?:\\.|[^\"\\])*\"|'(?:\\.|[^'\\])*'|[^\s,;&}\]]+)"
)
_EXCEPTION_KEYS = frozenset({"exc_info", "exception", "stack_info", "traceback", "stack"})


def _exception_type(value: Any) -> str | None:
    if isinstance(value, BaseException):
        return type(value).__name__
    if value is True:
        exception_class = sys.exc_info()[0]
        return exception_class.__name__ if exception_class else None
    if isinstance(value, tuple) and value and isinstance(value[0], type):
        if issubclass(value[0], BaseException):
            return value[0].__name__
    return None


def _redact(value: Any) -> Any:
    if isinstance(value, Mapping):
        sanitized = {}
        for key, item in value.items():
            safe_key = str(key) if isinstance(key, (str, int)) else "[key]"
            if _SENSITIVE_KEY.search(safe_key):
                sanitized[safe_key] = "[REDACTED]"
            elif safe_key.casefold() in _EXCEPTION_KEYS:
                sanitized[safe_key] = "[exception details suppressed]"
                exception_type = _exception_type(item)
                if exception_type:
                    sanitized["exception_type"] = exception_type
            else:
                sanitized[safe_key] = _redact(item)
        return sanitized
    if isinstance(value, (list, tuple)):
        return [_redact(item) for item in value]
    if isinstance(value, str):
        text = _AUTH_CREDENTIAL.sub(
            r"\1 [REDACTED]", _URL_CREDENTIAL.sub(r"\1[REDACTED]@", value)
        )
        return _KEY_VALUE_CREDENTIAL.sub(r"\1[REDACTED]", text)
    if isinstance(value, BaseException):
        return {"exception_type": type(value).__name__}
    if value is None or isinstance(value, (bool, int, float)):
        return value
    # Arbitrary repr/default=str can expose credentials in client objects.
    return f"[{type(value).__name__}]"


def redact_sensitive(_: Any, __: str, event_dict: EventDict) -> EventDict:
    sanitized: EventDict = _redact(event_dict)
    event_dict.clear()
    event_dict.update(sanitized)
    return event_dict


class RedactingJsonFormatter(logging.Formatter):
    """Last-mile protection for stdlib and structured console/file records.

    Exception messages, traceback source lines, stack dumps, and arbitrary
    object reprs never reach a handler. Known credential encodings in ordinary
    diagnostic text are redacted, but callers must still avoid logging secrets.
    """

    def format(self, record: logging.LogRecord) -> str:
        try:
            return self._format_safely(record)
        except Exception as exc:
            # logging.Handler.handleError can print the original message/args if
            # a formatter raises. A malformed or cyclic record must fail closed.
            return json.dumps(
                {
                    "event": "log record suppressed because safe formatting failed",
                    "error": type(exc).__name__,
                }
            )

    def _format_safely(self, record: logging.LogRecord) -> str:
        if isinstance(record.msg, str):
            message = record.msg
            if record.args:
                safe_arguments = (
                    {key: _redact(value) for key, value in record.args.items()}
                    if isinstance(record.args, Mapping)
                    else tuple(_redact(value) for value in record.args)
                )
                try:
                    message = message % safe_arguments
                except (TypeError, ValueError, KeyError):
                    message = "log message formatting failed; arguments suppressed"
            try:
                parsed = json.loads(message)
            except (ValueError, TypeError):
                parsed = None
            payload = parsed if isinstance(parsed, dict) else {"event": message}
        else:
            payload = {"event": _redact(record.msg)}
        payload.setdefault("level", record.levelname.lower())
        payload.setdefault("logger", record.name)
        payload.setdefault("timestamp", datetime.fromtimestamp(record.created, UTC).isoformat())
        exception_type = _exception_type(record.exc_info)
        if exception_type:
            payload["exception_type"] = exception_type
        if record.stack_info or record.exc_text:
            payload["exception_details"] = "suppressed"
        return json.dumps(_redact(payload), sort_keys=True, ensure_ascii=True, allow_nan=False)


def configure_json_logging(log_file: Path, level: int = logging.INFO) -> None:
    log_file.parent.mkdir(parents=True, exist_ok=True)
    handler = RotatingFileHandler(
        log_file, maxBytes=10 * 1024 * 1024, backupCount=10, encoding="utf-8"
    )
    formatter = RedactingJsonFormatter()
    handler.setFormatter(formatter)
    root = logging.getLogger()
    for previous in root.handlers:
        if isinstance(previous.formatter, RedactingJsonFormatter):
            previous.close()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(level)
    # Libraries such as server runners can own non-propagating stream handlers.
    # Apply the same boundary to existing console/file sinks, not just root.
    for logger in logging.Logger.manager.loggerDict.values():
        if isinstance(logger, logging.Logger):
            for existing in logger.handlers:
                if isinstance(existing, logging.StreamHandler):
                    existing.setFormatter(formatter)
    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso", utc=True),
            redact_sensitive,
            structlog.processors.JSONRenderer(sort_keys=True),
        ],
        logger_factory=structlog.stdlib.LoggerFactory(),
        cache_logger_on_first_use=True,
    )
