"""Explicit capability-metadata capture, never account or business-tool execution."""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import logging
import os
import re
import sys
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal
from uuid import uuid4

from mcp.shared.auth import OAuthClientMetadata
from mcp.types import ListToolsResult
from pydantic import BaseModel, ConfigDict, Field

from app.broker.mcp_session import ProtectedOAuthTokenStorage, ReadOnlyMcpSession
from app.broker.robinhood_mcp import MCP_STREAMABLE_HTTP_ENDPOINT
from app.exceptions import AuthenticationRequiredError, DataInvalidError
from app.security.credential_store import ProtectedCredentialStore, WindowsDpapiCredentialStore

MAX_TOOLS = 500
MAX_TOOL_BYTES = 256 * 1024
MAX_CATALOG_BYTES = 4 * 1024 * 1024
SNAPSHOT_VERSION = "robinhood-mcp-capabilities-v1"


def _canonical(value: object) -> bytes:
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False
    ).encode("utf-8")


def _validate_tree(value: object) -> None:
    # Check complexity before recursive JSON serialization. Schema documents are
    # data only: no $ref resolution, expression evaluation or URL fetch occurs.
    pending = [(value, 0)]
    count = 0
    while pending:
        item, depth = pending.pop()
        count += 1
        if count > 50000 or depth > 32:
            raise DataInvalidError("MCP schema complexity exceeds the snapshot bound")
        if isinstance(item, dict):
            if len(item) + len(pending) > 50000:
                raise DataInvalidError("MCP schema complexity exceeds the snapshot bound")
            for key, child in item.items():
                if not isinstance(key, str) or len(key) > 2048:
                    raise DataInvalidError("MCP schema contains an invalid key")
                pending.append((child, depth + 1))
        elif isinstance(item, list):
            if len(item) + len(pending) > 50000:
                raise DataInvalidError("MCP schema complexity exceeds the snapshot bound")
            pending.extend((child, depth + 1) for child in item)
        elif isinstance(item, str):
            if len(item) > 65536:
                raise DataInvalidError("MCP schema string exceeds the snapshot bound")
        elif item is not None and type(item) not in {int, float, bool}:
            raise DataInvalidError("MCP schema contains a non-JSON value")


class CapabilitySnapshot(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    version: Literal["robinhood-mcp-capabilities-v1"] = "robinhood-mcp-capabilities-v1"
    endpoint: Literal["https://agent.robinhood.com/mcp/trading"] = (
        "https://agent.robinhood.com/mcp/trading"
    )
    observed_at: datetime
    schema_hash: str = Field(pattern=r"^[a-f0-9]{64}$")
    tools: tuple[dict[str, Any], ...]
    qualified: Literal[False] = False
    external_write_authority: Literal[False] = False


def build_snapshot(listing: ListToolsResult, *, observed_at: datetime) -> CapabilitySnapshot:
    if observed_at.tzinfo is None or observed_at.utcoffset() is None:
        raise DataInvalidError("Capability snapshot timestamp must be timezone-aware")
    if listing.result_type != "complete" or listing.next_cursor is not None:
        raise DataInvalidError("Capability snapshot requires a complete unpaginated result")
    if not listing.tools or len(listing.tools) > MAX_TOOLS:
        raise DataInvalidError("Capability snapshot requires a nonempty bounded catalog")
    names: set[str] = set()
    tools: list[dict[str, Any]] = []
    for tool in listing.tools:
        if re.fullmatch(r"[A-Za-z0-9_.:-]{1,128}", tool.name) is None or tool.name in names:
            raise DataInvalidError("Capability snapshot has an invalid or duplicate tool name")
        names.add(tool.name)
        # Retain only capability metadata. Exclude opaque meta/icons/execution
        # extensions, account records, OAuth material and arbitrary response bodies.
        document = {
            "name": tool.name,
            "title": tool.title,
            "description": tool.description,
            "input_schema": tool.input_schema,
            "output_schema": tool.output_schema,
            "annotations": tool.annotations.model_dump(mode="json") if tool.annotations else None,
        }
        _validate_tree(document)
        try:
            encoded = _canonical(document)
        except (TypeError, ValueError, OverflowError):
            raise DataInvalidError("Capability schema is not finite canonical JSON") from None
        if len(encoded) > MAX_TOOL_BYTES:
            raise DataInvalidError("Capability schema exceeds the per-tool size bound")
        # Detach dictionaries from mutable SDK objects and reject non-finite values.
        tools.append(json.loads(encoded))
    tools.sort(key=lambda document: document["name"])
    payload = {
        "version": SNAPSHOT_VERSION,
        "endpoint": MCP_STREAMABLE_HTTP_ENDPOINT,
        "tools": tools,
    }
    encoded_catalog = _canonical(payload)
    if len(encoded_catalog) > MAX_CATALOG_BYTES:
        raise DataInvalidError("Capability catalog exceeds the total snapshot size bound")
    return CapabilitySnapshot(
        observed_at=observed_at.astimezone(UTC),
        schema_hash=hashlib.sha256(encoded_catalog).hexdigest(),
        tools=tuple(tools),
    )


async def capture_capabilities(
    store: ProtectedCredentialStore, *, timeout_seconds: float = 45
) -> dict[str, str | int | bool]:
    """Run only when explicitly invoked; empty allowlist denies all call_tool names."""
    if not 1 <= timeout_seconds <= 120:
        raise ValueError("Capability inspection timeout must be between 1 and 120 seconds")
    previous_logging = logging.root.manager.disable
    logging.disable(logging.CRITICAL)
    try:
        async with asyncio.timeout(timeout_seconds):
            storage = ProtectedOAuthTokenStorage(store)
            await storage.require_service_credentials()
            client_info = await storage.get_client_info()
            if client_info is None:
                raise AuthenticationRequiredError("Protected OAuth client is missing")
            metadata = OAuthClientMetadata(
                redirect_uris=client_info.redirect_uris,
                client_name="Options Sentinel capability inspection",
            )
            async with ReadOnlyMcpSession(
                storage=storage,
                client_metadata=metadata,
                allowed_tools=frozenset(),
                max_tool_pages=20,
                max_tools=MAX_TOOLS,
                request_timeout_seconds=min(20, timeout_seconds),
            ) as session:
                listing = await session.list_tools()
                snapshot = build_snapshot(listing, observed_at=datetime.now(UTC))
            # Metadata may contain arbitrary hostile text: store it privately,
            # never print it or feed it to an instruction/decision channel.
            key = (
                "mcp-capabilities-" + snapshot.observed_at.strftime("%Y%m%dT%H%M%SZ-") + uuid4().hex
            )
            if await asyncio.to_thread(store.load, key) is not None:
                raise DataInvalidError("Capability snapshot identifier unexpectedly exists")
            await asyncio.to_thread(store.save, key, snapshot.model_dump(mode="json"))
            saved = await asyncio.to_thread(store.load, key)
            if saved != snapshot.model_dump(mode="json"):
                raise DataInvalidError("Protected capability snapshot verification failed")
        return {
            "snapshot_saved": True,
            "tool_count": len(snapshot.tools),
            "schema_hash": snapshot.schema_hash,
            "qualified": False,
            "external_write_authority": False,
        }
    except Exception:
        raise DataInvalidError(
            "Capability inspection failed closed. Check protected credentials, authorization "
            "expiry, connectivity and catalog bounds. Remote details are suppressed."
        ) from None
    finally:
        logging.disable(previous_logging)


def cli(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Capture private MCP capability schemas only")
    parser.add_argument("command", choices=["inspect"])
    parser.add_argument("--timeout-seconds", type=int, default=45)
    arguments = parser.parse_args(argv)
    if os.name != "nt":
        print("Capability inspection requires native Windows DPAPI storage.", file=sys.stderr)
        return 2
    try:
        store = WindowsDpapiCredentialStore(Path.home() / ".options-sentinel" / "oauth")
        result = asyncio.run(capture_capabilities(store, timeout_seconds=arguments.timeout_seconds))
        print(json.dumps(result, sort_keys=True))
        return 0
    except KeyboardInterrupt:
        print("Capability inspection cancelled; no trading was enabled.", file=sys.stderr)
        return 2
    except Exception:
        print(
            "Capability inspection failed closed. Authorize separately if credentials expired. "
            "Remote data and credential details are suppressed; no trading was enabled.",
            file=sys.stderr,
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(cli())
