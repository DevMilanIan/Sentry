from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from collections.abc import Sequence
from pathlib import Path

import uvicorn
from alembic import command
from alembic.config import Config as AlembicConfig

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
    alembic.set_main_option("sqlalchemy.url", url.replace("%", "%%"))
    command.upgrade(alembic, "head")


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


def cli(argv: Sequence[str] | None = None) -> int:
    arguments = _parser().parse_args(argv)
    try:
        loaded = load_config(arguments.config_dir)
        if arguments.command == "validate-config":
            print(loaded.bind_runtime().model_dump_json(indent=2))
            return 0
        if arguments.command == "database-upgrade":
            _upgrade_database(loaded.app.database.url)
            return 0
        if arguments.command == "demo-once":
            print(canonical_json(asyncio.run(_demo_once(arguments.config_dir))))
            return 0
        if arguments.command == "serve":
            # Migrations are mandatory; failure aborts startup rather than silently using memory.
            binding = loaded.bind_runtime()
            configure_json_logging(binding.runtime_directory / "logs" / "sentinel.jsonl")
            _upgrade_database(loaded.app.database.url)
            asyncio.run(_serve(loaded, arguments.host, arguments.port))
            return 0
    except ConfigurationError as exc:
        print(json.dumps({"error": "configuration", "detail": str(exc)}), file=sys.stderr)
        return 2
    except Exception as exc:
        print(json.dumps({"error": type(exc).__name__, "detail": str(exc)}), file=sys.stderr)
        return 1
    return 1


if __name__ == "__main__":
    raise SystemExit(cli())
