"""Explicit, native Windows OAuth setup; never imported by service startup.

The pinned MCP SDK performs discovery, PKCE, issuer/state validation and token
exchange. This module supplies a bounded loopback browser handoff, protected
storage, and narrower network/operation policy. No account or order tool runs.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import math
import os
import re
import secrets
import sys
import time
from collections.abc import AsyncGenerator, Awaitable, Callable, Sequence
from pathlib import Path
from types import TracebackType
from urllib.parse import parse_qs, urlsplit

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
from pydantic import AnyUrl

from app.broker.mcp_session import ProtectedOAuthTokenStorage
from app.broker.robinhood_mcp import MCP_STREAMABLE_HTTP_ENDPOINT
from app.exceptions import AuthenticationRequiredError
from app.security.credential_store import WindowsDpapiCredentialStore

CALLBACK_PATH = "/oauth/callback"
MAX_RESPONSE_BYTES = 1024 * 1024


def _reject(detail: str = "OAuth setup rejected an unsupported or unsafe response") -> None:
    raise AuthenticationRequiredError(detail)


def _robinhood_https(url: str) -> None:
    parsed = urlsplit(url)
    host = parsed.hostname or ""
    if (
        parsed.scheme != "https"
        or not (host == "robinhood.com" or host.endswith(".robinhood.com"))
        or parsed.port not in {None, 443}
        or parsed.username is not None
        or parsed.password is not None
        or parsed.fragment
        or any(ord(character) < 33 or ord(character) == 127 for character in url)
        or len(url) > 16384
    ):
        _reject("OAuth discovery requires reviewed Robinhood HTTPS endpoints")


def _callback_port(client: OAuthClientInformationFull | None) -> int:
    if client is None:
        return 0
    if not client.redirect_uris or len(client.redirect_uris) != 1:
        _reject("Saved OAuth registration has unsupported redirect metadata")
    assert client.redirect_uris is not None
    uri = urlsplit(str(client.redirect_uris[0]))
    if (
        uri.scheme != "http"
        or uri.hostname != "127.0.0.1"
        or uri.port is None
        or not 1024 <= uri.port <= 65535
        or uri.path != CALLBACK_PATH
        or uri.query
        or uri.fragment
        or uri.username is not None
    ):
        _reject("Saved OAuth registration has unsupported redirect metadata")
    assert uri.port is not None
    return uri.port


class LoopbackCallback:
    """One ephemeral listener, no access logs, bounded headers and connections."""

    def __init__(self, *, port: int = 0) -> None:
        self._port = port
        self._server: asyncio.Server | None = None
        self._connections: set[asyncio.Task[None]] = set()
        self._authorization_url: str | None = None
        self._state: str | None = None
        self._launched = False
        self._result: asyncio.Future[AuthorizationCodeResult] | None = None

    @property
    def origin(self) -> str:
        if self._server is None:
            raise RuntimeError("OAuth callback listener is not active")
        return f"http://127.0.0.1:{self._server.sockets[0].getsockname()[1]}"

    @property
    def redirect_uri(self) -> str:
        return self.origin + CALLBACK_PATH

    async def __aenter__(self) -> LoopbackCallback:
        self._result = asyncio.get_running_loop().create_future()
        self._server = await asyncio.start_server(
            self._accept, "127.0.0.1", self._port, limit=8192, backlog=4
        )
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        if self._server:
            self._server.close()
            await self._server.wait_closed()
        for connection in tuple(self._connections):
            connection.cancel()
        await asyncio.gather(*self._connections, return_exceptions=True)
        if self._result and not self._result.done():
            self._result.cancel()
        self._authorization_url = self._state = None

    def _accept(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        if len(self._connections) >= 4:
            writer.close()
            return
        task = asyncio.create_task(self._handle(reader, writer))
        self._connections.add(task)
        task.add_done_callback(self._connections.discard)

    async def prepare(self, url: str, opener: Callable[[str], Awaitable[None]]) -> None:
        _robinhood_https(url)
        fields = parse_qs(urlsplit(url).query, strict_parsing=True, max_num_fields=20)
        if (
            self._authorization_url is not None
            or any(len(value) != 1 for value in fields.values())
            or fields.get("redirect_uri") != [self.redirect_uri]
            or fields.get("response_type") != ["code"]
            or fields.get("code_challenge_method") != ["S256"]
            or not re.fullmatch(r"[A-Za-z0-9_-]{43,128}", fields.get("code_challenge", [""])[0])
            or not re.fullmatch(r"[A-Za-z0-9_-]{32,256}", fields.get("state", [""])[0])
        ):
            _reject("OAuth authorization request failed PKCE/callback validation")
        self._state = fields["state"][0]
        self._authorization_url = url
        # Only this non-secret loopback URL reaches ShellExecute/browser argv.
        # The actual URL, state and PKCE challenge travel in an HTTP Location.
        await opener(self.origin + "/authorize")

    async def result(self) -> AuthorizationCodeResult:
        if self._result is None:
            raise RuntimeError("OAuth callback listener is not active")
        return await self._result

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            async with asyncio.timeout(5):
                raw = await reader.readuntil(b"\r\n\r\n")
                lines = raw.decode("ascii").split("\r\n")
                method, target, version = lines[0].split(" ")
                headers: dict[str, str] = {}
                for line in lines[1:-2]:
                    name, value = line.split(":", 1)
                    name = name.lower()
                    if name in headers:
                        raise ValueError("duplicate header")
                    headers[name] = value.strip()
                if (
                    method != "GET"
                    or version != "HTTP/1.1"
                    or headers.get("host") != urlsplit(self.origin).netloc
                    or "origin" in headers
                    or "transfer-encoding" in headers
                    or headers.get("content-length", "0") != "0"
                    or not target.startswith("/")
                    or target.startswith("//")
                    or len(raw) > 8192
                ):
                    raise ValueError("invalid request")
                status, location, body = self._route(target)
                output = (
                    f"HTTP/1.1 {status}\r\nContent-Type: text/plain; charset=utf-8\r\n"
                    "Cache-Control: no-store\r\nReferrer-Policy: no-referrer\r\n"
                    "Content-Security-Policy: default-src 'none'; frame-ancestors 'none'\r\n"
                    "X-Content-Type-Options: nosniff\r\nConnection: close\r\n"
                    + (f"Location: {location}\r\n" if location else "")
                    + f"Content-Length: {len(body)}\r\n\r\n{body}"
                )
                writer.write(output.encode("ascii"))
                await writer.drain()
        except (
            ValueError,
            UnicodeError,
            asyncio.IncompleteReadError,
            asyncio.LimitOverrunError,
            TimeoutError,
            ConnectionError,
        ):
            # No arbitrary request, callback code, state or exception is logged.
            pass
        finally:
            writer.close()
            try:
                async with asyncio.timeout(1):
                    await writer.wait_closed()
            except (TimeoutError, ConnectionError):
                pass

    def _route(self, target: str) -> tuple[str, str | None, str]:
        if target == "/authorize" and self._authorization_url and not self._launched:
            self._launched = True
            return "302 Found", self._authorization_url, "Continue in your browser."
        parsed = urlsplit(target)
        if parsed.path != CALLBACK_PATH or parsed.fragment or not self._state:
            return "404 Not Found", None, "Not found."
        fields = parse_qs(parsed.query, strict_parsing=True, max_num_fields=12)
        if (
            any(len(value) != 1 or len(value[0]) > 4096 for value in fields.values())
            or not secrets.compare_digest(fields.get("state", [""])[0], self._state)
            or self._result is None
            or self._result.done()
        ):
            return "400 Bad Request", None, "Invalid or completed callback."
        if "error" in fields:
            self._result.set_exception(AuthenticationRequiredError("OAuth consent was denied"))
            return "400 Bad Request", None, "Authorization declined. Return to the terminal."
        if not fields.get("code", [""])[0]:
            return "400 Bad Request", None, "Missing authorization code."
        self._result.set_result(
            AuthorizationCodeResult(
                code=fields["code"][0], state=fields["state"][0], iss=fields.get("iss", [None])[0]
            )
        )
        return "200 OK", None, "Callback received. Return to the terminal for the result."


class _StagedStorage:
    """Keep existing credentials intact until an authenticated handshake succeeds."""

    def __init__(
        self, target: ProtectedOAuthTokenStorage, client: OAuthClientInformationFull | None
    ) -> None:
        self.target, self.original_client = target, client
        self.client = client
        self.tokens: OAuthToken | None = None
        self._tokens_received_at: float | None = None

    async def get_tokens(self) -> OAuthToken | None:
        # A user-invoked renewal always rediscovers metadata, never guesses a
        # refresh endpoint from an old token and never grants service authority.
        return None

    async def get_client_info(self) -> OAuthClientInformationFull | None:
        return self.client

    async def set_client_info(self, client_info: OAuthClientInformationFull) -> None:
        if self.original_client is not None and client_info != self.original_client:
            _reject("OAuth issuer/registration changed; separate reviewed migration required")
        self.client = client_info.model_copy(deep=True)

    async def set_tokens(self, tokens: OAuthToken) -> None:
        self.tokens = tokens.model_copy(deep=True)
        self._tokens_received_at = time.monotonic()

    async def commit(self) -> None:
        if self.client is None or self.tokens is None:
            _reject("OAuth handshake did not produce complete protected credentials")
        assert self.client is not None and self.tokens is not None
        if self.tokens.expires_in is not None and self._tokens_received_at is not None:
            self.tokens.expires_in -= math.ceil(time.monotonic() - self._tokens_received_at)
        if self.tokens.expires_in is not None and self.tokens.expires_in <= 0:
            _reject("OAuth server returned expired credentials")
        if self.original_client is None:
            await self.target.set_client_info(self.client)
        await self.target.set_tokens(self.tokens)
        await self.target.require_service_credentials()


class _BootstrapProvider(OAuthClientProvider):
    def __init__(
        self,
        storage: _StagedStorage,
        callback: LoopbackCallback,
        opener: Callable[[str], Awaitable[None]],
    ) -> None:
        self._callback, self._opener = callback, opener
        super().__init__(
            MCP_STREAMABLE_HTTP_ENDPOINT,
            OAuthClientMetadata(
                redirect_uris=[AnyUrl(callback.redirect_uri)],
                client_name="Options Sentinel native read-only setup",
                token_endpoint_auth_method="none",  # noqa: S106
                application_type="native",
                grant_types=["authorization_code", "refresh_token"],
            ),
            storage,
            redirect_handler=self._redirect,
            callback_handler=callback.result,
        )

    def _metadata_required(self) -> None:
        metadata = self.context.oauth_metadata
        if self.context.protected_resource_metadata is None or metadata is None:
            _reject("OAuth server lacks required discovery metadata; no endpoint fallback allowed")
        assert metadata is not None
        if "S256" not in (metadata.code_challenge_methods_supported or []):
            _reject("OAuth server does not advertise required S256 PKCE support")
        for endpoint in (metadata.issuer, metadata.authorization_endpoint, metadata.token_endpoint):
            _robinhood_https(str(endpoint))

    async def _redirect(self, url: str) -> None:
        self._metadata_required()
        assert self.context.oauth_metadata is not None
        expected = urlsplit(str(self.context.oauth_metadata.authorization_endpoint))
        actual = urlsplit(url)
        if (actual.scheme, actual.netloc, actual.path) != (
            expected.scheme,
            expected.netloc,
            expected.path,
        ):
            _reject("OAuth authorization endpoint differs from discovered metadata")
        client = self.context.client_info
        if (
            client is None
            or not client.redirect_uris
            or (AnyUrl(self._callback.redirect_uri) not in client.redirect_uris)
        ):
            _reject("OAuth registration did not preserve the required loopback callback")
        await self._callback.prepare(url, self._opener)

    def _validate_request(self, request: httpx2.Request) -> None:
        url = str(request.url)
        _robinhood_https(url)
        if url == MCP_STREAMABLE_HTTP_ENDPOINT:
            if request.method == "POST":
                body = json.loads(request.content)
                if not isinstance(body, dict) or body.get("method") not in {
                    "server/discover",
                    "initialize",
                    "notifications/initialized",
                }:
                    _reject("OAuth bootstrap cannot transmit MCP tools or business operations")
            elif request.method not in {"GET", "DELETE"}:
                _reject()
            return
        if request.method == "GET":
            if request.headers.get("authorization") or request.url.query:
                _reject()
            if self.context.protected_resource_metadata is None:
                # A challenged metadata URL is allowed only on the fixed
                # resource host; the SDK verifies its resource binding next.
                if request.url.host != "agent.robinhood.com":
                    _reject()
            return
        self._metadata_required()
        assert self.context.oauth_metadata is not None
        metadata = self.context.oauth_metadata
        if request.method != "POST" or url not in {
            str(metadata.registration_endpoint) if metadata.registration_endpoint else "",
            str(metadata.token_endpoint),
        }:
            _reject("OAuth server requires unsupported client registration; no guessed endpoints")

    async def async_auth_flow(
        self, request: httpx2.Request
    ) -> AsyncGenerator[httpx2.Request, httpx2.Response]:
        flow = super().async_auth_flow(request)
        try:
            outgoing = await anext(flow)
            count = 0
            while True:
                count += 1
                if count > 20:
                    _reject("OAuth discovery/request count exceeded the setup bound")
                self._validate_request(outgoing)
                response = yield outgoing
                if response.status_code == 403:
                    _reject("OAuth access was denied; automatic scope escalation is disabled")
                try:
                    outgoing = await flow.asend(response)
                except StopAsyncIteration:
                    return
        finally:
            await flow.aclose()


class _BoundedStream(httpx2.AsyncByteStream):
    def __init__(self, stream: httpx2.AsyncByteStream) -> None:
        self.stream = stream

    async def __aiter__(self) -> AsyncGenerator[bytes, None]:
        count = 0
        async for chunk in self.stream:
            count += len(chunk)
            if count > MAX_RESPONSE_BYTES:
                _reject("OAuth response exceeded the setup size bound")
            yield chunk

    async def aclose(self) -> None:
        await self.stream.aclose()


class _BoundedTransport(httpx2.AsyncBaseTransport):
    def __init__(self, inner: httpx2.AsyncBaseTransport) -> None:
        self.inner = inner

    async def handle_async_request(self, request: httpx2.Request) -> httpx2.Response:
        response = await self.inner.handle_async_request(request)
        if response.headers.get("content-encoding", "identity").lower() != "identity":
            await response.aclose()
            _reject("OAuth setup requires uncompressed bounded responses")
        assert isinstance(response.stream, httpx2.AsyncByteStream)
        response.stream = _BoundedStream(response.stream)
        return response

    async def aclose(self) -> None:
        await self.inner.aclose()


async def _open_browser(url: str) -> None:
    # No shell, subprocess, authorization URL, tokens or codes in arguments.
    os.startfile(url)  # noqa: S606


async def authorize(
    storage: ProtectedOAuthTokenStorage,
    *,
    opener: Callable[[str], Awaitable[None]] = _open_browser,
    timeout_seconds: float = 300,
    transport: httpx2.AsyncBaseTransport | None = None,
) -> dict[str, bool]:
    if not 1 <= timeout_seconds <= 600:
        raise ValueError("OAuth setup timeout must be between 1 and 600 seconds")
    # The SDK includes exception response bodies/URLs in some diagnostics.
    # This is a dedicated CLI process, not a service API: suppress all library
    # logs during the flow and return controlled, credential-free outcomes only.
    prior_logging = logging.root.manager.disable
    logging.disable(logging.CRITICAL)
    try:
        async with asyncio.timeout(timeout_seconds):
            client = await storage.get_client_info()
            staged = _StagedStorage(storage, client)
            async with LoopbackCallback(port=_callback_port(client)) as callback:
                provider = _BootstrapProvider(staged, callback, opener)
                async with httpx2.AsyncClient(
                    auth=provider,
                    timeout=httpx2.Timeout(20),
                    headers={"Accept-Encoding": "identity"},
                    trust_env=False,
                    follow_redirects=False,
                    transport=_BoundedTransport(
                        transport or httpx2.AsyncHTTPTransport(trust_env=False, retries=0)
                    ),
                ) as http:
                    async with Client(
                        streamable_http_client(MCP_STREAMABLE_HTTP_ENDPOINT, http_client=http),
                        read_timeout_seconds=timeout_seconds,
                        cache=None,
                        input_required_max_rounds=0,
                    ):
                        # The handshake is the ONLY MCP operation. There is no
                        # generic call_tool, account read, order or runtime wiring.
                        pass
                await staged.commit()
        return {"authorization_saved": True, "account_qualified": False, "trading_enabled": False}
    except Exception:
        raise AuthenticationRequiredError(
            "OAuth setup did not complete. Credentials and server details are suppressed. "
            "Check browser consent, callback availability and supported discovery/registration."
        ) from None
    finally:
        logging.disable(prior_logging)


def cli(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Explicit native Robinhood OAuth bootstrap")
    parser.add_argument("command", choices=["authorize"])
    parser.add_argument("--timeout-seconds", type=int, default=300)
    args = parser.parse_args(argv)
    if os.name != "nt" or not sys.stdin.isatty():
        print("OAuth setup requires an interactive native Windows terminal.", file=sys.stderr)
        return 2
    print(
        "This opens Robinhood consent for the intended Agentic account. Review its permissions "
        "yourself; the token may have broader broker scopes than this setup command uses. "
        "Setup sends only an MCP handshake, never account/order tools, enables no trading, "
        "and changes no environment or funding gates. Never paste passwords or tokens here."
    )
    try:
        if input("Type AUTHORIZE to continue, or anything else to cancel: ").strip() != "AUTHORIZE":
            print("OAuth setup cancelled; no network connection opened.")
            return 2
        directory = Path.home() / ".options-sentinel" / "oauth"
        store = WindowsDpapiCredentialStore(directory)
        result = asyncio.run(
            authorize(ProtectedOAuthTokenStorage(store), timeout_seconds=args.timeout_seconds)
        )
        print(json.dumps(result, sort_keys=True))
        return 0
    except KeyboardInterrupt:
        print("OAuth setup cancelled; no trading was enabled.", file=sys.stderr)
        return 2
    except Exception:
        print(
            "OAuth setup failed closed. Check private storage, browser consent, callback port, "
            "and server support for discovered registration and S256 PKCE. "
            "No trading was enabled; sensitive details are suppressed.",
            file=sys.stderr,
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(cli())
