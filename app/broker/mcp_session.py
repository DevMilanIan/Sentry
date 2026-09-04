"""Official MCP 2.1.1 session building blocks; never a live-write transport.

Construction is inert. Entering a session requires previously protected OAuth
material and is an explicit network operation. Runtime wiring, account/schema
qualification, unattended token refresh, and interactive authorization remain
separate work; none is inferred from having these objects available.
"""

from __future__ import annotations

import asyncio
import math
import re
from collections.abc import AsyncGenerator, Awaitable, Callable, Mapping
from contextlib import AsyncExitStack
from datetime import UTC, datetime
from types import TracebackType
from typing import Any, Literal

import httpx2
from mcp import Client
from mcp.client.auth import OAuthClientProvider
from mcp.client.streamable_http import streamable_http_client
from mcp.shared.auth import (
    AuthorizationCodeResult,
    OAuthClientInformationFull,
    OAuthClientMetadata,
    OAuthToken,
)
from mcp.types import CallToolResult, ListToolsResult, Tool
from pydantic import BaseModel, ConfigDict, field_validator

from app.broker.robinhood_mcp import MCP_STREAMABLE_HTTP_ENDPOINT
from app.exceptions import AuthenticationRequiredError, DataInvalidError, SafetyCriticalError
from app.security.credential_store import ProtectedCredentialStore

# Names come from the existing read/review facade. A server annotation never
# adds a name to this policy. Optional names still require explicit selection
# and schema/account verification by the caller before use.
SUPPORTED_READ_ONLY_TOOLS = frozenset(
    {
        "get_accounts",
        "get_portfolio",
        "get_account_state",
        "get_account",
        "get_account_details",
        "get_account_profile",
        "get_option_positions",
        "get_positions",
        "list_option_positions",
        "get_option_orders",
        "get_orders",
        "list_option_orders",
        "review_option_order",
    }
)
DEFAULT_READ_ONLY_TOOLS = frozenset({"get_accounts", "get_portfolio"})


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _aware_utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("OAuth storage clock must be timezone-aware")
    return value.astimezone(UTC)


class _ProtectedRecord(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    endpoint: str = MCP_STREAMABLE_HTTP_ENDPOINT

    @field_validator("endpoint")
    @classmethod
    def fixed_endpoint(cls, value: str) -> str:
        if value != MCP_STREAMABLE_HTTP_ENDPOINT:
            raise ValueError("OAuth record belongs to another endpoint")
        return value


class _TokenRecord(_ProtectedRecord):
    version: Literal["protected-mcp-token-v1"] = "protected-mcp-token-v1"
    saved_at: datetime
    tokens: OAuthToken

    _validate_saved_at = field_validator("saved_at")(_aware_utc)


class _ClientRecord(_ProtectedRecord):
    version: Literal["protected-mcp-client-v1"] = "protected-mcp-client-v1"
    client_info: OAuthClientInformationFull


class ProtectedOAuthTokenStorage:
    """SDK TokenStorage bridge with no plaintext fallback or secret-bearing repr.

    Allocate one protected key prefix per explicitly selected intended account.
    Reusing/changing that selection requires separate account qualification; a
    prefix is storage isolation, not proof of account ownership or authority.
    Persisting save time prevents process restarts from renewing token lifetime.
    """

    def __init__(
        self,
        store: ProtectedCredentialStore,
        *,
        key_prefix: str = "robinhood-agentic-mcp",
        now: Callable[[], datetime] = _utc_now,
    ) -> None:
        if re.fullmatch(r"[A-Za-z0-9_-]{1,80}", key_prefix) is None:
            raise ValueError("invalid OAuth credential key prefix")
        self._store, self._prefix, self._now = store, key_prefix, now
        self._lock = asyncio.Lock()

    async def get_tokens(self) -> OAuthToken | None:
        async with self._lock:
            try:
                raw = await asyncio.to_thread(self._store.load, f"{self._prefix}-tokens")
                if raw is None:
                    return None
                record = _TokenRecord.model_validate(raw)
                now = _aware_utc(self._now())
                if now < record.saved_at or not record.tokens.access_token:
                    raise ValueError("invalid protected token state")
                tokens = record.tokens.model_copy(deep=True)
                if tokens.expires_in is not None:
                    remaining = tokens.expires_in - (now - record.saved_at).total_seconds()
                    tokens.expires_in = max(0, math.floor(remaining))
                return tokens
            except Exception:
                raise AuthenticationRequiredError(
                    "protected OAuth token record is unavailable"
                ) from None

    async def set_tokens(self, tokens: OAuthToken) -> None:
        async with self._lock:
            try:
                if not tokens.access_token or (
                    tokens.expires_in is not None and tokens.expires_in < 0
                ):
                    raise ValueError("invalid OAuth token")
                record = _TokenRecord(saved_at=_aware_utc(self._now()), tokens=tokens)
                await asyncio.to_thread(
                    self._store.save,
                    f"{self._prefix}-tokens",
                    record.model_dump(mode="json"),
                )
            except Exception:
                raise AuthenticationRequiredError(
                    "protected OAuth token record could not be saved"
                ) from None

    async def get_client_info(self) -> OAuthClientInformationFull | None:
        async with self._lock:
            try:
                raw = await asyncio.to_thread(self._store.load, f"{self._prefix}-client")
                if raw is None:
                    return None
                client = _ClientRecord.model_validate(raw).client_info
                if not client.client_id:
                    raise ValueError("invalid OAuth client")
                return client.model_copy(deep=True)
            except Exception:
                raise AuthenticationRequiredError(
                    "protected OAuth client record is unavailable"
                ) from None

    async def set_client_info(self, client_info: OAuthClientInformationFull) -> None:
        async with self._lock:
            try:
                if not client_info.client_id:
                    raise ValueError("invalid OAuth client")
                record = _ClientRecord(client_info=client_info)
                await asyncio.to_thread(
                    self._store.save,
                    f"{self._prefix}-client",
                    record.model_dump(mode="json"),
                )
            except Exception:
                raise AuthenticationRequiredError(
                    "protected OAuth client record could not be saved"
                ) from None

    async def require_service_credentials(self) -> None:
        tokens = await self.get_tokens()
        client = await self.get_client_info()
        if (
            tokens is None
            or client is None
            or (tokens.expires_in is not None and tokens.expires_in <= 0)
        ):
            raise AuthenticationRequiredError(
                "existing unexpired OAuth credentials are required; authorize separately"
            )


async def _deny_redirect(_: str) -> None:
    raise AuthenticationRequiredError("interactive OAuth is disabled during service operation")


async def _deny_callback() -> AuthorizationCodeResult:
    raise AuthenticationRequiredError("interactive OAuth is disabled during service operation")


class _NoninteractiveOAuthProvider(OAuthClientProvider):
    """Stop challenged requests before SDK discovery/registration/interactive flow."""

    # A service request only needs the status code. Do not inherit the SDK's
    # OAuth-discovery body buffering, which would consume an open MCP SSE stream.
    requires_response_body = False

    def __init__(
        self, storage: ProtectedOAuthTokenStorage, client_metadata: OAuthClientMetadata
    ) -> None:
        self._protected_storage = storage
        super().__init__(
            MCP_STREAMABLE_HTTP_ENDPOINT,
            client_metadata.model_copy(deep=True),
            storage,
            redirect_handler=_deny_redirect,
            callback_handler=_deny_callback,
        )

    async def async_auth_flow(
        self, request: httpx2.Request
    ) -> AsyncGenerator[httpx2.Request, httpx2.Response]:
        await self._protected_storage.require_service_credentials()
        tokens = await self._protected_storage.get_tokens()
        if tokens is None or (tokens.expires_in is not None and tokens.expires_in <= 0):
            raise AuthenticationRequiredError("MCP authorization expired; authorize separately")
        # Do not delegate to SDK auth here: a token can expire between our
        # precheck and the SDK's second load, triggering its refresh flow before
        # the challenged-request guard. Service operation sends exactly one
        # fixed-resource request, never discovery, refresh or authorization.
        if str(request.url) != MCP_STREAMABLE_HTTP_ENDPOINT:
            raise AuthenticationRequiredError("MCP service credentials require the fixed endpoint")
        request.headers["Authorization"] = f"Bearer {tokens.access_token}"
        response = yield request
        if response.status_code in {401, 403}:
            raise AuthenticationRequiredError(
                "MCP authorization expired or denied; authorize separately"
            )


def create_noninteractive_oauth_provider(
    storage: ProtectedOAuthTokenStorage, client_metadata: OAuthClientMetadata
) -> OAuthClientProvider:
    """Build an inert provider; expired/challenged credentials never start OAuth."""
    return _NoninteractiveOAuthProvider(storage, client_metadata)


def create_interactive_oauth_provider(
    storage: ProtectedOAuthTokenStorage,
    client_metadata: OAuthClientMetadata,
    *,
    redirect_handler: Callable[[str], Awaitable[None]],
    callback_handler: Callable[[], Awaitable[AuthorizationCodeResult]],
) -> OAuthClientProvider:
    """Build only for a separately user-invoked auth flow; performs no I/O itself.

    The caller must provide the approved metadata and callback implementation.
    No redirect URI, scopes, browser launch, callback listener, or account schema
    is guessed here. This provider cannot be injected into ReadOnlyMcpSession.
    """
    if not callable(redirect_handler) or not callable(callback_handler):
        raise ValueError("explicit OAuth callbacks are required")
    return OAuthClientProvider(
        MCP_STREAMABLE_HTTP_ENDPOINT,
        client_metadata.model_copy(deep=True),
        storage,
        redirect_handler=redirect_handler,
        callback_handler=callback_handler,
    )


class ReadOnlyMcpSession:
    """Single-use, same-loop SDK session implementing the read facade transport.

    The exact fixed endpoint and name allowlist are independent of untrusted
    tool annotations. Listing may expose write schemas for local validation,
    but this object cannot transmit a write tool. No roots/sampling/elicitation
    callbacks or automatic input-required retries are enabled.
    """

    def __init__(
        self,
        *,
        storage: ProtectedOAuthTokenStorage,
        client_metadata: OAuthClientMetadata,
        allowed_tools: frozenset[str] = DEFAULT_READ_ONLY_TOOLS,
        max_tool_pages: int = 20,
        max_tools: int = 500,
        request_timeout_seconds: float = 30,
    ) -> None:
        if not allowed_tools.issubset(SUPPORTED_READ_ONLY_TOOLS):
            raise SafetyCriticalError("MCP tool allowlist contains unknown or mutating names")
        if (
            type(max_tool_pages) is not int
            or type(max_tools) is not int
            or not 1 <= max_tool_pages <= 100
            or not 1 <= max_tools <= 5000
            or not math.isfinite(request_timeout_seconds)
            or not 0 < request_timeout_seconds <= 60
        ):
            raise ValueError("invalid MCP discovery/request bounds")
        self._storage = storage
        self._metadata = client_metadata.model_copy(deep=True)
        self._allowed = frozenset(allowed_tools)
        # An empty set is intentionally supported for capability-only sessions:
        # listing schemas grants no authority to invoke any discovered tool.
        self._max_pages, self._max_tools = max_tool_pages, max_tools
        self._timeout = request_timeout_seconds
        self._client: Client | None = None
        self._stack: AsyncExitStack | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._owner: asyncio.Task[Any] | None = None
        self._used = False
        self._operation_lock = asyncio.Lock()
        self._discovered: dict[str, Tool] = {}

    async def __aenter__(self) -> ReadOnlyMcpSession:
        if self._used:
            raise RuntimeError("MCP session is single-use and cannot be reentered")
        self._used = True
        self._loop, self._owner = asyncio.get_running_loop(), asyncio.current_task()
        await self._storage.require_service_credentials()
        stack = AsyncExitStack()
        await stack.__aenter__()
        try:
            http = await stack.enter_async_context(
                httpx2.AsyncClient(
                    auth=create_noninteractive_oauth_provider(self._storage, self._metadata),
                    timeout=httpx2.Timeout(self._timeout, read=self._timeout),
                    follow_redirects=False,
                    trust_env=False,
                )
            )
            transport = streamable_http_client(
                MCP_STREAMABLE_HTTP_ENDPOINT, http_client=http, terminate_on_close=True
            )
            self._client = await stack.enter_async_context(
                Client(
                    transport,
                    read_timeout_seconds=self._timeout,
                    cache=None,
                    input_required_max_rounds=0,
                )
            )
            self._stack = stack
            return self
        except BaseException:
            await stack.aclose()
            self._client = None
            raise

    def _active_client(self) -> Client:
        if self._client is None or self._loop is not asyncio.get_running_loop():
            raise RuntimeError("MCP session must be active on its owning event loop")
        return self._client

    async def list_tools(self) -> ListToolsResult:
        self._active_client()
        async with self._operation_lock, asyncio.timeout(self._timeout):
            client = self._active_client()
            self._discovered.clear()
            await self._storage.require_service_credentials()
            tools: dict[str, Tool] = {}
            cursor: str | None = None
            cursors: set[str] = set()
            for _ in range(self._max_pages):
                page = await client.list_tools(cursor=cursor, cache_mode="bypass")
                if page.result_type != "complete":
                    raise DataInvalidError("MCP tool listing is not complete")
                for tool in page.tools:
                    if not tool.name or tool.name in tools:
                        raise DataInvalidError("MCP tool listing has missing or duplicate names")
                    if len(tools) >= self._max_tools:
                        raise DataInvalidError("MCP tool count exceeded discovery bound")
                    tools[tool.name] = tool.model_copy(deep=True)
                cursor = page.next_cursor
                if cursor is None:
                    self._discovered = tools
                    return ListToolsResult(
                        tools=[tool.model_copy(deep=True) for tool in tools.values()]
                    )
                if not cursor or len(cursor) > 4096 or cursor in cursors:
                    raise DataInvalidError("MCP tool pagination cursor is invalid or repeated")
                cursors.add(cursor)
            raise DataInvalidError("MCP tool pagination exceeded discovery bound")

    async def call_tool(self, name: str, arguments: Mapping[str, Any]) -> CallToolResult:
        # Check names before all client activity, even before session validation.
        if name not in self._allowed:
            raise SafetyCriticalError("MCP tool is not in the fixed read-only allowlist")
        self._active_client()
        async with self._operation_lock, asyncio.timeout(self._timeout):
            client = self._active_client()
            tool = self._discovered.get(name)
            if tool is None:
                raise SafetyCriticalError("MCP read tool requires completed fresh discovery")
            if tool.annotations and (
                tool.annotations.destructive_hint is True
                or tool.annotations.read_only_hint is False
            ):
                raise SafetyCriticalError("MCP read tool declares unsafe annotations")
            await self._storage.require_service_credentials()
            return await client.call_tool(name, dict(arguments), read_timeout_seconds=self._timeout)

    async def close(self) -> None:
        if self._stack is None:
            return
        self._active_client()
        if asyncio.current_task() is not self._owner:
            raise RuntimeError("MCP session must close in its entering task")
        async with self._operation_lock:
            stack, self._stack = self._stack, None
            self._client = None
            self._discovered.clear()
            await stack.aclose()

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: TracebackType | None,
    ) -> None:
        await self.close()
