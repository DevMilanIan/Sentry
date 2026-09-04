from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from copy import deepcopy
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx2
import pytest
from mcp.shared.auth import (
    AuthorizationCodeResult,
    OAuthClientInformationFull,
    OAuthClientMetadata,
    OAuthToken,
)
from mcp.types import CallToolResult, ListToolsResult, Tool, ToolAnnotations
from pydantic import AnyUrl

from app.broker import mcp_session
from app.broker.mcp_session import (
    ProtectedOAuthTokenStorage,
    ReadOnlyMcpSession,
    create_interactive_oauth_provider,
    create_noninteractive_oauth_provider,
)
from app.broker.robinhood_mcp import MCP_STREAMABLE_HTTP_ENDPOINT
from app.exceptions import AuthenticationRequiredError, DataInvalidError, SafetyCriticalError

NOW = datetime(2026, 9, 3, 12, tzinfo=UTC)


class MemoryProtectedStore:
    """Explicit test double only, never selected by production construction."""

    def __init__(self) -> None:
        self.records: dict[str, dict[str, Any]] = {}
        self.failure: Exception | None = None

    def save(self, key: str, value: dict[str, Any]) -> None:
        if self.failure:
            raise self.failure
        self.records[key] = deepcopy(value)

    def load(self, key: str) -> dict[str, Any] | None:
        if self.failure:
            raise self.failure
        return deepcopy(self.records.get(key))

    def delete(self, key: str) -> None:
        self.records.pop(key, None)


def metadata() -> OAuthClientMetadata:
    return OAuthClientMetadata(
        redirect_uris=[AnyUrl("http://127.0.0.1:8765/callback")],
        client_name="Fixture client",
        token_endpoint_auth_method="none",
    )


async def seeded_storage(
    now: Any = lambda: NOW,
) -> tuple[MemoryProtectedStore, ProtectedOAuthTokenStorage]:
    protected = MemoryProtectedStore()
    storage = ProtectedOAuthTokenStorage(protected, now=now)
    await storage.set_tokens(
        OAuthToken(
            access_token="fixture-access-secret", refresh_token="fixture-refresh", expires_in=300
        )
    )
    await storage.set_client_info(
        OAuthClientInformationFull(
            client_id="fixture-client", client_secret="fixture-client-secret"
        )
    )
    return protected, storage


def tool(name: str, **annotations: Any) -> Tool:
    return Tool(
        name=name,
        input_schema={"type": "object", "properties": {}},
        annotations=ToolAnnotations(**annotations) if annotations else None,
    )


@dataclass
class FakeSdk:
    pages: list[ListToolsResult] = field(default_factory=list)
    events: list[str] = field(default_factory=list)
    list_calls: list[dict[str, Any]] = field(default_factory=list)
    tool_calls: list[tuple[str, dict[str, Any], dict[str, Any]]] = field(default_factory=list)
    http_options: dict[str, Any] = field(default_factory=dict)
    client_options: dict[str, Any] = field(default_factory=dict)
    transport_options: dict[str, Any] = field(default_factory=dict)
    fail_enter: bool = False
    list_delay: float = 0

    def install(self, monkeypatch: pytest.MonkeyPatch) -> None:
        state = self

        class Http:
            def __init__(self, **kwargs: Any) -> None:
                state.http_options = kwargs
                state.events.append("http_construct")

            async def __aenter__(self) -> Http:
                state.events.append("http_enter")
                return self

            async def __aexit__(self, *_: object) -> None:
                state.events.append("http_exit")

        @asynccontextmanager
        async def transport(url: str, **kwargs: Any) -> Any:
            state.transport_options = {"url": url, **kwargs}
            state.events.append("transport_enter")
            try:
                yield (object(), object())
            finally:
                state.events.append("transport_exit")

        class SdkClient:
            def __init__(self, server: Any, **kwargs: Any) -> None:
                state.client_options = kwargs
                self.transport = server

            async def __aenter__(self) -> SdkClient:
                if state.fail_enter:
                    raise ConnectionError("fixture handshake failed")
                await self.transport.__aenter__()
                state.events.append("client_enter")
                return self

            async def __aexit__(self, *args: Any) -> None:
                state.events.append("client_exit")
                await self.transport.__aexit__(*args)

            async def list_tools(self, **kwargs: Any) -> ListToolsResult:
                state.list_calls.append(kwargs)
                if state.list_delay:
                    await asyncio.sleep(state.list_delay)
                return state.pages.pop(0)

            async def call_tool(
                self, name: str, arguments: dict[str, Any], **kwargs: Any
            ) -> CallToolResult:
                state.tool_calls.append((name, arguments, kwargs))
                return CallToolResult(content=[], structured_content={"fixture": True})

        monkeypatch.setattr(mcp_session.httpx2, "AsyncClient", Http)
        monkeypatch.setattr(mcp_session, "streamable_http_client", transport)
        monkeypatch.setattr(mcp_session, "Client", SdkClient)


async def test_token_bridge_round_trip_isolation_and_elapsed_expiry() -> None:
    current = NOW
    protected, storage = await seeded_storage(lambda: current)
    token = await storage.get_tokens()
    assert token and token.expires_in == 300
    current += timedelta(seconds=30, milliseconds=100)
    restored = ProtectedOAuthTokenStorage(protected, now=lambda: current)
    token = await restored.get_tokens()
    assert token and token.expires_in == 269
    client = await restored.get_client_info()
    assert client and client.client_id == "fixture-client"
    other = ProtectedOAuthTokenStorage(protected, key_prefix="another-account", now=lambda: current)
    assert await other.get_tokens() is None
    assert await other.get_client_info() is None
    assert "fixture-access-secret" not in repr(storage)
    assert "fixture-client-secret" not in repr(storage)


@pytest.mark.parametrize("prefix", ["", "../account", "a/b", "a b", "x" * 81])
def test_unsafe_credential_key_is_rejected(prefix: str) -> None:
    with pytest.raises(ValueError, match="key prefix"):
        ProtectedOAuthTokenStorage(MemoryProtectedStore(), key_prefix=prefix)


@pytest.mark.parametrize("mutation", ["malformed", "endpoint", "naive_saved", "future_saved"])
async def test_invalid_protected_token_record_is_sanitized(mutation: str) -> None:
    protected, storage = await seeded_storage()
    record = protected.records["robinhood-agentic-mcp-tokens"]
    if mutation == "malformed":
        record["tokens"] = {"access_token": {"secret": "fixture-access-secret"}}
    elif mutation == "endpoint":
        record["endpoint"] = "https://unapproved.example/mcp"
    elif mutation == "naive_saved":
        record["saved_at"] = "2026-09-03T12:00:00"
    else:
        record["saved_at"] = "2026-09-03T12:00:01Z"
    with pytest.raises(AuthenticationRequiredError) as error:
        await storage.get_tokens()
    assert "fixture-access-secret" not in str(error.value)
    assert error.value.__suppress_context__


async def test_naive_injected_clock_and_storage_errors_never_leak_material() -> None:
    protected, storage = await seeded_storage(lambda: NOW)
    naive = ProtectedOAuthTokenStorage(protected, now=lambda: NOW.replace(tzinfo=None))
    with pytest.raises(AuthenticationRequiredError):
        await naive.get_tokens()
    with pytest.raises(AuthenticationRequiredError):
        await naive.set_tokens(OAuthToken(access_token="fixture-access-secret"))
    protected.failure = OSError("fixture-access-secret from provider failure")
    for operation in (
        storage.get_tokens,
        storage.get_client_info,
        lambda: storage.set_tokens(OAuthToken(access_token="fixture-access-secret")),
        lambda: storage.set_client_info(OAuthClientInformationFull(client_id="fixture-client")),
    ):
        with pytest.raises(AuthenticationRequiredError) as error:
            await operation()
        assert "fixture-access-secret" not in str(error.value)


@pytest.mark.parametrize("missing", ["tokens", "client", "expired"])
async def test_missing_expired_credentials_block_before_network_factory(
    monkeypatch: pytest.MonkeyPatch, missing: str
) -> None:
    protected, storage = await seeded_storage()
    if missing == "expired":
        await storage.set_tokens(OAuthToken(access_token="fixture", expires_in=0))
    else:
        protected.delete(f"robinhood-agentic-mcp-{missing}")
    sdk = FakeSdk()
    sdk.install(monkeypatch)
    with pytest.raises(AuthenticationRequiredError):
        async with ReadOnlyMcpSession(storage=storage, client_metadata=metadata()):
            pytest.fail("session must not open")
    assert not sdk.events


async def test_oauth_factories_inert_and_service_callbacks_refuse_interaction() -> None:
    _, storage = await seeded_storage()
    provider = create_noninteractive_oauth_provider(storage, metadata())
    assert "fixture-access-secret" not in repr(provider)
    assert provider.context.redirect_handler and provider.context.callback_handler
    with pytest.raises(AuthenticationRequiredError):
        await provider.context.redirect_handler("https://fixture.invalid/authorize")
    with pytest.raises(AuthenticationRequiredError):
        await provider.context.callback_handler()
    calls: list[str] = []

    async def redirect(url: str) -> None:
        calls.append(url)

    async def callback() -> AuthorizationCodeResult:
        calls.append("callback")
        return AuthorizationCodeResult(code="fixture-code", state="fixture-state")

    interactive = create_interactive_oauth_provider(
        storage, metadata(), redirect_handler=redirect, callback_handler=callback
    )
    assert calls == []
    assert interactive.context.redirect_handler is redirect
    assert interactive.context.callback_handler is callback


@pytest.mark.parametrize("status_code", [401, 403])
async def test_service_oauth_challenge_stops_before_discovery(status_code: int) -> None:
    _, storage = await seeded_storage()
    provider = create_noninteractive_oauth_provider(storage, metadata())
    request = httpx2.Request("POST", MCP_STREAMABLE_HTTP_ENDPOINT)
    flow = provider.async_auth_flow(request)
    outgoing = await anext(flow)
    assert outgoing.url == request.url
    assert outgoing.headers["Authorization"] == "Bearer fixture-access-secret"
    with pytest.raises(AuthenticationRequiredError, match="authorize separately"):
        await flow.asend(httpx2.Response(status_code, request=request))
    await flow.aclose()


async def test_service_oauth_success_uses_only_original_request() -> None:
    _, storage = await seeded_storage()
    provider = create_noninteractive_oauth_provider(storage, metadata())
    assert provider.requires_response_body is False
    request = httpx2.Request("POST", MCP_STREAMABLE_HTTP_ENDPOINT)
    flow = provider.async_auth_flow(request)
    assert await anext(flow) is request
    with pytest.raises(StopAsyncIteration):
        await flow.asend(httpx2.Response(200, request=request))


async def test_noninteractive_expiry_race_cannot_trigger_sdk_refresh(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, storage = await seeded_storage()
    original_get = storage.get_tokens
    loads = 0

    async def expires_on_second_load() -> OAuthToken | None:
        nonlocal loads
        loads += 1
        token = await original_get()
        assert token
        if loads > 1:
            token.expires_in = 0
        return token

    monkeypatch.setattr(storage, "get_tokens", expires_on_second_load)
    provider = create_noninteractive_oauth_provider(storage, metadata())
    flow = provider.async_auth_flow(httpx2.Request("POST", MCP_STREAMABLE_HTTP_ENDPOINT))
    with pytest.raises(AuthenticationRequiredError, match="authorize separately"):
        await anext(flow)
    assert loads == 2
    await flow.aclose()


async def test_noninteractive_token_cannot_be_attached_to_another_endpoint() -> None:
    _, storage = await seeded_storage()
    provider = create_noninteractive_oauth_provider(storage, metadata())
    request = httpx2.Request("GET", "https://agent.robinhood.com/arbitrary")
    flow = provider.async_auth_flow(request)
    with pytest.raises(AuthenticationRequiredError, match="fixed endpoint"):
        await anext(flow)
    assert "Authorization" not in request.headers
    await flow.aclose()


async def test_empty_allowlist_allows_metadata_but_never_tool_calls(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, storage = await seeded_storage()
    sdk = FakeSdk(pages=[ListToolsResult(tools=[tool("get_accounts")])])
    sdk.install(monkeypatch)
    async with ReadOnlyMcpSession(
        storage=storage, client_metadata=metadata(), allowed_tools=frozenset()
    ) as session:
        assert len((await session.list_tools()).tools) == 1
        for name in ("get_accounts", "get_portfolio", "place_option_order", "anything"):
            with pytest.raises(SafetyCriticalError, match="allowlist"):
                await session.call_tool(name, {})
    assert not sdk.tool_calls


async def test_sdk_session_paginated_uncached_and_clean_shutdown(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, storage = await seeded_storage()
    sdk = FakeSdk(
        pages=[
            ListToolsResult(tools=[tool("get_accounts")], next_cursor="next"),
            ListToolsResult(tools=[tool("place_option_order", read_only_hint=True)]),
        ]
    )
    sdk.install(monkeypatch)
    session = ReadOnlyMcpSession(storage=storage, client_metadata=metadata())
    assert sdk.events == []
    async with session:
        listing = await session.list_tools()
        assert listing.next_cursor is None
        assert [item.name for item in listing.tools] == ["get_accounts", "place_option_order"]
        result = await session.call_tool("get_accounts", {})
        assert result.structured_content == {"fixture": True}
        for name in (
            "place_option_order",
            "cancel_option_order",
            "unknown_read",
            "create_watchlist",
        ):
            with pytest.raises(SafetyCriticalError, match="allowlist"):
                await session.call_tool(name, {})
    assert sdk.list_calls == [
        {"cursor": None, "cache_mode": "bypass"},
        {"cursor": "next", "cache_mode": "bypass"},
    ]
    assert len(sdk.tool_calls) == 1
    assert sdk.client_options == {
        "read_timeout_seconds": 30,
        "cache": None,
        "input_required_max_rounds": 0,
    }
    assert sdk.http_options["follow_redirects"] is False
    assert sdk.http_options["trust_env"] is False
    assert sdk.transport_options["url"] == MCP_STREAMABLE_HTTP_ENDPOINT
    assert sdk.transport_options["terminate_on_close"] is True
    assert sdk.events[-3:] == ["client_exit", "transport_exit", "http_exit"]
    await session.close()
    with pytest.raises(RuntimeError, match="single-use"):
        async with session:
            pass


@pytest.mark.parametrize("tools", [frozenset({"place_option_order"}), frozenset({"new_get"})])
async def test_constructor_cannot_expand_static_allowlist(tools: frozenset[str]) -> None:
    _, storage = await seeded_storage()
    with pytest.raises(SafetyCriticalError):
        ReadOnlyMcpSession(storage=storage, client_metadata=metadata(), allowed_tools=tools)


@pytest.mark.parametrize("annotations", [{"destructive_hint": True}, {"read_only_hint": False}])
async def test_unsafe_annotation_blocks_even_allowlisted_name(
    monkeypatch: pytest.MonkeyPatch, annotations: dict[str, bool]
) -> None:
    _, storage = await seeded_storage()
    sdk = FakeSdk(pages=[ListToolsResult(tools=[tool("get_accounts", **annotations)])])
    sdk.install(monkeypatch)
    async with ReadOnlyMcpSession(storage=storage, client_metadata=metadata()) as session:
        with pytest.raises(SafetyCriticalError, match="discovery"):
            await session.call_tool("get_accounts", {})
        await session.list_tools()
        with pytest.raises(SafetyCriticalError, match="unsafe annotations"):
            await session.call_tool("get_accounts", {})
        with pytest.raises(SafetyCriticalError, match="discovery"):
            await session.call_tool("get_portfolio", {})
    assert sdk.tool_calls == []


@pytest.mark.parametrize("failure", ["duplicate", "loop", "pages", "count"])
async def test_discovery_bounds_never_publish_partial_catalog(
    monkeypatch: pytest.MonkeyPatch, failure: str
) -> None:
    _, storage = await seeded_storage()
    first = ListToolsResult(tools=[tool("get_accounts")], next_cursor="a")
    second = ListToolsResult(tools=[tool("get_portfolio")])
    options: dict[str, Any] = {}
    if failure == "duplicate":
        second = ListToolsResult(tools=[tool("get_accounts")])
    elif failure == "loop":
        second = ListToolsResult(tools=[tool("get_portfolio")], next_cursor="a")
    elif failure == "pages":
        options["max_tool_pages"] = 1
    else:
        options["max_tools"] = 1
    sdk = FakeSdk(pages=[first, second])
    sdk.install(monkeypatch)
    async with ReadOnlyMcpSession(
        storage=storage, client_metadata=metadata(), **options
    ) as session:
        with pytest.raises(DataInvalidError):
            await session.list_tools()
        with pytest.raises(SafetyCriticalError, match="discovery"):
            await session.call_tool("get_accounts", {})
    assert not sdk.tool_calls


async def test_failed_refresh_invalidates_previously_discovered_tools(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, storage = await seeded_storage()
    sdk = FakeSdk(
        pages=[
            ListToolsResult(tools=[tool("get_accounts")]),
            ListToolsResult(tools=[tool("get_accounts"), tool("get_accounts")]),
        ]
    )
    sdk.install(monkeypatch)
    async with ReadOnlyMcpSession(storage=storage, client_metadata=metadata()) as session:
        listing = await session.list_tools()
        listing.tools[0].annotations = ToolAnnotations(destructive_hint=True)
        await session.call_tool("get_accounts", {})  # caller mutation cannot alter policy snapshot
        with pytest.raises(DataInvalidError):
            await session.list_tools()
        with pytest.raises(SafetyCriticalError):
            await session.call_tool("get_accounts", {})


async def test_session_timeout_and_handshake_failure_close_http(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, storage = await seeded_storage()
    sdk = FakeSdk(fail_enter=True)
    sdk.install(monkeypatch)
    with pytest.raises(ConnectionError):
        async with ReadOnlyMcpSession(storage=storage, client_metadata=metadata()):
            pass
    assert sdk.events == ["http_construct", "http_enter", "http_exit"]
    sdk.fail_enter = False
    sdk.list_delay = 1
    async with ReadOnlyMcpSession(
        storage=storage, client_metadata=metadata(), request_timeout_seconds=0.01
    ) as session:
        with pytest.raises(TimeoutError):
            await session.list_tools()
    assert sdk.events[-3:] == ["client_exit", "transport_exit", "http_exit"]


async def test_same_loop_and_entering_task_ownership(monkeypatch: pytest.MonkeyPatch) -> None:
    _, storage = await seeded_storage()
    sdk = FakeSdk()
    sdk.install(monkeypatch)
    async with ReadOnlyMcpSession(storage=storage, client_metadata=metadata()) as session:
        with pytest.raises(RuntimeError, match="entering task"):
            await asyncio.create_task(session.close())

        def another_loop() -> None:
            asyncio.run(session.list_tools())

        with pytest.raises(RuntimeError, match="owning event loop"):
            await asyncio.to_thread(another_loop)
    assert not sdk.list_calls


@pytest.mark.parametrize(
    "bounds",
    [
        {"max_tool_pages": 0},
        {"max_tools": 0},
        {"request_timeout_seconds": 0},
        {"request_timeout_seconds": float("inf")},
        {"request_timeout_seconds": 61},
    ],
)
async def test_invalid_bounds_rejected(bounds: dict[str, Any]) -> None:
    _, storage = await seeded_storage()
    with pytest.raises(ValueError, match="bounds"):
        ReadOnlyMcpSession(storage=storage, client_metadata=metadata(), **bounds)
