from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import get_args

import uvicorn
from alembic import command
from alembic.config import Config as AlembicConfig
from pydantic import BaseModel, ValidationError

from app.config import LoadedConfig, load_config
from app.demo.offline_scenario import run_offline_demo_scenario
from app.domain.models import canonical_json
from app.exceptions import ConfigurationError
from app.observability.logging import configure_json_logging
from app.runtime import build_application


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="options-sentinel")
    parser.add_argument("--config-dir", type=Path, default=Path("config"))
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser(
        "validate-config", help="validate configuration and print immutable binding"
    )
    commands.add_parser("database-upgrade", help="run Alembic migrations")
    commands.add_parser("demo-once", help="run one credential-free deterministic smoke cycle")
    serve = commands.add_parser("serve", help="start the local dashboard and controller")
    serve.add_argument("--host", default=None)
    serve.add_argument("--port", type=int, default=None)
    return parser


def _upgrade_database(url: str) -> None:
    alembic = AlembicConfig("alembic.ini")
    # Embedded migrations must not replace the application's redacting/rotating
    # handlers or disable loggers already constructed during application imports.
    alembic.attributes["configure_logger"] = False
    alembic.set_main_option("sqlalchemy.url", url.replace("%", "%%"))
    command.upgrade(alembic, "head")


def _validate_migration_schemas(loaded: LoadedConfig) -> None:
    # The shipped revisions explicitly create shared/demo/live. Refuse a
    # custom-schema CLI deployment before connecting or migrating another schema.
    schemas = (
        (loaded.app.database.shared_schema, "shared"),
        (loaded.app.database.demo_schema, "demo"),
        (loaded.app.database.live_schema, "live"),
        (loaded.demo.database_schema, "demo"),
        (loaded.broker_shadow.database_schema, "demo"),
        (loaded.live.database_schema, "live"),
    )
    if any(actual != supported for actual, supported in schemas):
        raise ConfigurationError(
            "production migrations support only shared/demo/live schemas; "
            "custom-schema deployment requires separately reviewed migrations"
        )


async def _demo_once(config_dir: Path) -> dict[str, object]:
    loaded = load_config(config_dir)
    binding = loaded.bind_runtime()
    if binding.environment.value != "DEMO" or binding.demo_backend is None:
        raise ConfigurationError("demo-once requires a DEMO startup profile")
    result = await run_offline_demo_scenario(config_dir, loaded=loaded)
    return {
        "binding": binding.model_dump(mode="json"),
        "configured_trading_mode": loaded.app.trading_mode.value,
        "bounded_scenario_mode": "AUTO",
        "external_write_authority": binding.external_write_authority,
        "result": result.model_dump(mode="json"),
    }


async def _serve(loaded: LoadedConfig, host: str | None, port: int | None) -> None:
    # Build clients, serve requests, and close clients on one event loop.
    runtime = await build_application(
        loaded, dashboard_token=os.getenv("SENTRY_DASHBOARD_TOKEN")
    )
    try:
        await uvicorn.Server(
            uvicorn.Config(
                runtime.application,
                host=host or loaded.app.dashboard.host,
                port=port or loaded.app.dashboard.port,
                log_config=None,
            )
        ).serve()
    finally:
        await runtime.close()


def _configuration_field_names() -> set[str]:
    fields: set[str] = set()
    seen: set[type[BaseModel]] = set()
    pending: list[object] = [LoadedConfig]
    while pending:
        annotation = pending.pop()
        if isinstance(annotation, type) and issubclass(annotation, BaseModel):
            if annotation not in seen:
                seen.add(annotation)
                fields.update(annotation.model_fields)
                pending.extend(field.annotation for field in annotation.model_fields.values())
        else:
            pending.extend(get_args(annotation))
    return fields


def _configuration_failure_detail(exc: ConfigurationError) -> str:
    # These are exact static application diagnostics, never prefixes followed
    # by user-controlled values, paths, YAML snippets, or connection strings.
    safe_messages = {
        "app and Demo profile backend mismatch",
        "demo-once requires a DEMO startup profile",
        "production migrations support only shared/demo/live schemas; "
        "custom-schema deployment requires separately reviewed migrations",
    }
    message = exc.args[0] if len(exc.args) == 1 and isinstance(exc.args[0], str) else None
    if message in safe_messages:
        return message
    cause: BaseException | None = exc
    for _ in range(8):
        if isinstance(cause, ValidationError):
            # Pydantic's default text includes input_value and exception context.
            # Keep only field names defined by our static model schemas. Mapping
            # keys from user input are replaced, even if they look like names.
            known_fields = _configuration_field_names()
            issues = cause.errors(include_url=False, include_context=False, include_input=False)
            fields = sorted(
                {
                    ".".join(
                        str(part) if part in known_fields else "[item]"
                        for part in issue["loc"]
                    ) or "[configuration]"
                    for issue in issues
                }
            )
            return (
                f"Configuration validation failed ({len(issues)} issues); "
                f"fields: {', '.join(fields[:10])}. Input values are suppressed."
            )
        cause = cause.__cause__ if cause else None
        if cause is None:
            break
    return "Configuration rejected. Check the configuration files; input values are suppressed."


def cli(argv: Sequence[str] | None = None) -> int:
    arguments = _parser().parse_args(argv)
    try:
        loaded = load_config(arguments.config_dir)
        if arguments.command == "validate-config":
            print(loaded.bind_runtime().model_dump_json(indent=2))
            return 0
        if arguments.command == "database-upgrade":
            _validate_migration_schemas(loaded)
            _upgrade_database(loaded.app.database.url)
            return 0
        if arguments.command == "demo-once":
            print(canonical_json(asyncio.run(_demo_once(arguments.config_dir))))
            return 0
        if arguments.command == "serve":
            # Migrations are mandatory; failure aborts startup rather than silently using memory.
            _validate_migration_schemas(loaded)
            binding = loaded.bind_runtime()
            configure_json_logging(binding.runtime_directory / "logs" / "sentinel.jsonl")
            _upgrade_database(loaded.app.database.url)
            asyncio.run(_serve(loaded, arguments.host, arguments.port))
            return 0
    except ConfigurationError as exc:
        print(
            json.dumps(
                {
                    "error": "configuration",
                    "exception_type": type(exc).__name__,
                    "detail": _configuration_failure_detail(exc),
                }
            ),
            file=sys.stderr,
        )
        return 2
    except Exception as exc:
        print(
            json.dumps(
                {
                    "error": type(exc).__name__,
                    "detail": (
                        "Command failed; exception details suppressed to protect credentials."
                    ),
                }
            ),
            file=sys.stderr,
        )
        return 1
    return 1


if __name__ == "__main__":
    raise SystemExit(cli())
