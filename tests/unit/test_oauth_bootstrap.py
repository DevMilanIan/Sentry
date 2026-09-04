from __future__ import annotations

import asyncio
import json
import logging
from copy import deepcopy
from typing import Any
from urllib.parse import parse_qs, urlencode, urlsplit

import httpx2
import pytest
from mcp.shared.auth import OAuthClientInformationFull, OAuthToken
from pydantic import AnyUrl

from app.broker import oauth_bootstrap as bootstrap
from app.broker.mcp_session import ProtectedOAuthTokenStorage
from app.broker.robinhood_mcp import MCP_STREAMABLE_HTTP_ENDPOINT
from app.exceptions import AuthenticationRequiredError


class MemoryStore:
    def __init__(self) -> None:
        self.records: dict[str, dict[str, Any]] = {}

    def save(self, key: str, value: dict[str, Any]) -> None:
        self.records[key] = deepcopy(value)

    def load(self, key: str) -> dict[str, Any] | None:
        return deepcopy(self.records.get(key))

    def delete(self, key: str) -> None:
        self.records.pop(key, None)


def authorization_url(callback: bootstrap.LoopbackCallback) -> str:
    return "https://agent.robinhood.com/oauth/authorize?" + urlencode(
        {
            "state": "x" * 43,
            "code_challenge": "y" * 43,
            "code_challenge_method": "S256",
            "response_type": "code",
            "redirect_uri": callback.redirect_uri,
        }
    )


async def test_loopback_handoff_state_and_duplicate_validation() -> None:
    opened: list[str] = []

    async def opener(url: str) -> None:
        opened.append(url)

    async with bootstrap.LoopbackCallback() as callback:
        url = authorization_url(callback)
        await callback.prepare(url, opener)
        assert opened == [callback.origin + "/authorize"]
        assert "state" not in opened[0] and "challenge" not in opened[0]
        async with httpx2.AsyncClient(trust_env=False) as http:
            redirect = await http.get(opened[0])
            assert redirect.status_code == 302 and redirect.headers["location"] == url
            assert redirect.headers["cache-control"] == "no-store"
            assert (await http.get(opened[0])).status_code == 404
            assert (
                await http.get(callback.redirect_uri + "?state=bad&code=secret")
            ).status_code == 400
            assert (
                await http.get(
                    callback.redirect_uri + "?state=" + "x" * 43 + "&state=bad&code=secret"
                )
            ).status_code == 400
            valid = callback.redirect_uri + "?state=" + "x" * 43 + "&code=fixture-code"
            assert (await http.get(valid)).status_code == 200
            result = await callback.result()
            assert result.code == "fixture-code"
            assert (await http.get(valid)).status_code == 400
        port = urlsplit(callback.origin).port
    with pytest.raises(OSError):
        await asyncio.open_connection("127.0.0.1", port)


@pytest.mark.parametrize(
    "url",
    [
        "http://agent.robinhood.com/x",
        "https://evil.test/x",
        "https://robinhood.com.evil.test/x",
        "https://evilrobinhood.com/x",
        "https://user@agent.robinhood.com/x",
        "https://agent.robinhood.com:444/x",
        "https://agent.robinhood.com/x#fragment",
        "https://agent.robinhood.com/x\r\nInjected: yes",
    ],
)
def test_endpoint_trust_policy(url: str) -> None:
    with pytest.raises(AuthenticationRequiredError):
        bootstrap._robinhood_https(url)


@pytest.mark.parametrize(
    "uri",
    [
        "http://localhost:9876/oauth/callback",
        "http://0.0.0.0:9876/oauth/callback",
        "http://127.0.0.1:80/oauth/callback",
        "http://127.0.0.1:9876/other",
        "http://127.0.0.1:9876/oauth/callback?secret=x",
        "https://127.0.0.1:9876/oauth/callback",
    ],
)
def test_saved_registration_only_exact_loopback(uri: str) -> None:
    with pytest.raises(AuthenticationRequiredError):
        bootstrap._callback_port(
            OAuthClientInformationFull(client_id="fixture", redirect_uris=[AnyUrl(uri)])
        )


class OAuthServerFixture:
    def __init__(self) -> None:
        self.requests: list[httpx2.Request] = []
        self.registration: dict[str, Any] | None = None
        self.mcp_methods: list[str] = []
        self.pkce = True
        self.registration_supported = True
        self.token_failure = False

    def handle(self, request: httpx2.Request) -> httpx2.Response:
        self.requests.append(request)
        url = str(request.url)
        if url == MCP_STREAMABLE_HTTP_ENDPOINT:
            if request.method in {"GET", "DELETE"}:
                return httpx2.Response(405)
            body = json.loads(request.content)
            self.mcp_methods.append(body["method"])
            if request.headers.get("authorization") != "Bearer fixture-access":
                return httpx2.Response(401, headers={"WWW-Authenticate": "Bearer"})
            if body["method"] == "server/discover":
                return httpx2.Response(
                    200,
                    json={
                        "jsonrpc": "2.0",
                        "id": body["id"],
                        "error": {"code": -32601, "message": "legacy"},
                    },
                )
            if body["method"] == "initialize":
                return httpx2.Response(
                    200,
                    json={
                        "jsonrpc": "2.0",
                        "id": body["id"],
                        "result": {
                            "protocolVersion": "2025-11-25",
                            "capabilities": {},
                            "serverInfo": {"name": "fixture", "version": "1"},
                        },
                    },
                )
            assert body["method"] == "notifications/initialized"
            return httpx2.Response(202)
        if "oauth-protected-resource" in url:
            return httpx2.Response(
                200,
                json={
                    "resource": MCP_STREAMABLE_HTTP_ENDPOINT,
                    "authorization_servers": ["https://agent.robinhood.com"],
                    "scopes_supported": ["fixture-read"],
                },
            )
        if ".well-known/" in url:
            metadata = {
                "issuer": "https://agent.robinhood.com",
                "authorization_endpoint": "https://agent.robinhood.com/oauth/authorize",
                "token_endpoint": "https://agent.robinhood.com/oauth/token",
                "code_challenge_methods_supported": ["S256"] if self.pkce else ["plain"],
            }
            if self.registration_supported:
                metadata["registration_endpoint"] = "https://agent.robinhood.com/oauth/register"
            return httpx2.Response(200, json=metadata)
        if url == "https://agent.robinhood.com/oauth/register":
            self.registration = json.loads(request.content)
            return httpx2.Response(201, json={**self.registration, "client_id": "fixture-client"})
        if url == "https://agent.robinhood.com/oauth/token":
            data = parse_qs(request.content.decode())
            assert data["grant_type"] == ["authorization_code"]
            assert data["code"] == ["fixture-code"]
            assert len(data["code_verifier"][0]) == 128
            if self.token_failure:
                return httpx2.Response(400, text="fixture-secret-must-not-escape")
            return httpx2.Response(
                200,
                json={
                    "access_token": "fixture-access",
                    "refresh_token": "fixture-refresh",
                    "token_type": "Bearer",
                    "expires_in": 300,
                },
            )
        raise AssertionError("unexpected outbound request")


async def automatic_fixture_browser(url: str) -> None:
    assert url.startswith("http://127.0.0.1:") and url.endswith("/authorize")
    async with httpx2.AsyncClient(trust_env=False) as http:
        launch = await http.get(url)
        fields = parse_qs(urlsplit(launch.headers["location"]).query)
        assert fields["code_challenge_method"] == ["S256"]
        callback = (
            fields["redirect_uri"][0]
            + "?"
            + urlencode(
                {
                    "code": "fixture-code",
                    "state": fields["state"][0],
                    "iss": "https://agent.robinhood.com",
                }
            )
        )
        assert (await http.get(callback)).status_code == 200


async def test_actual_sdk_discovery_pkce_handshake_and_protected_commit() -> None:
    fixture = OAuthServerFixture()
    store = MemoryStore()
    storage = ProtectedOAuthTokenStorage(store)
    result = await bootstrap.authorize(
        storage,
        opener=automatic_fixture_browser,
        transport=httpx2.MockTransport(fixture.handle),
        timeout_seconds=10,
    )
    assert result == {
        "authorization_saved": True,
        "account_qualified": False,
        "trading_enabled": False,
    }
    assert set(fixture.mcp_methods) <= {
        "server/discover",
        "initialize",
        "notifications/initialized",
    }
    assert "initialize" in fixture.mcp_methods
    assert fixture.registration is not None
    assert fixture.registration["application_type"] == "native"
    assert len(store.records) == 2
    tokens = await storage.get_tokens()
    assert tokens and tokens.access_token == "fixture-access"

    saved_client = await storage.get_client_info()
    registrations = len(
        [request for request in fixture.requests if str(request.url).endswith("/oauth/register")]
    )
    # Explicit renewal reuses the saved redirect/client, while rediscovering
    # metadata and completing a fresh browser authorization-code flow.
    renewed = await bootstrap.authorize(
        storage,
        opener=automatic_fixture_browser,
        transport=httpx2.MockTransport(fixture.handle),
        timeout_seconds=10,
    )
    assert renewed["authorization_saved"]
    assert await storage.get_client_info() == saved_client
    assert (
        len(
            [
                request
                for request in fixture.requests
                if str(request.url).endswith("/oauth/register")
            ]
        )
        == registrations
    )


async def test_issuer_mismatch_never_reaches_token_endpoint() -> None:
    fixture = OAuthServerFixture()
    store = MemoryStore()

    async def wrong_issuer_browser(url: str) -> None:
        async with httpx2.AsyncClient(trust_env=False) as http:
            launch = await http.get(url)
            fields = parse_qs(urlsplit(launch.headers["location"]).query)
            callback = (
                fields["redirect_uri"][0]
                + "?"
                + urlencode(
                    {
                        "code": "fixture-code",
                        "state": fields["state"][0],
                        "iss": "https://evil.test",
                    }
                )
            )
            await http.get(callback)

    with pytest.raises(AuthenticationRequiredError):
        await bootstrap.authorize(
            ProtectedOAuthTokenStorage(store),
            opener=wrong_issuer_browser,
            transport=httpx2.MockTransport(fixture.handle),
            timeout_seconds=10,
        )
    assert not store.records
    assert not any(str(request.url).endswith("/oauth/token") for request in fixture.requests)


async def test_oversized_response_is_rejected_without_storage() -> None:
    store = MemoryStore()

    def oversized(request: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(401, content=b"x" * (bootstrap.MAX_RESPONSE_BYTES + 1))

    with pytest.raises(AuthenticationRequiredError):
        await bootstrap.authorize(
            ProtectedOAuthTokenStorage(store),
            opener=automatic_fixture_browser,
            transport=httpx2.MockTransport(oversized),
            timeout_seconds=10,
        )
    assert not store.records


async def test_consent_denial_preserves_storage() -> None:
    fixture = OAuthServerFixture()
    store = MemoryStore()

    async def denied_browser(url: str) -> None:
        async with httpx2.AsyncClient(trust_env=False) as http:
            launch = await http.get(url)
            fields = parse_qs(urlsplit(launch.headers["location"]).query)
            callback = (
                fields["redirect_uri"][0]
                + "?"
                + urlencode(
                    {
                        "error": "access_denied",
                        "error_description": "sensitive-remote-text",
                        "state": fields["state"][0],
                    }
                )
            )
            assert (await http.get(callback)).status_code == 400

    with pytest.raises(AuthenticationRequiredError):
        await bootstrap.authorize(
            ProtectedOAuthTokenStorage(store),
            opener=denied_browser,
            transport=httpx2.MockTransport(fixture.handle),
            timeout_seconds=10,
        )
    assert not store.records


@pytest.mark.parametrize(
    "header",
    [
        "Host: evil.test",
        "Host: 127.0.0.1:123\r\nHost: duplicate",
        "Host: {host}\r\nOrigin: https://evil.test",
        "Host: {host}\r\nContent-Length: 1",
        "Host: {host}\r\nTransfer-Encoding: chunked",
    ],
)
async def test_callback_rejects_invalid_http_headers(header: str) -> None:
    async with bootstrap.LoopbackCallback() as callback:
        address = urlsplit(callback.origin)
        reader, writer = await asyncio.open_connection("127.0.0.1", address.port)
        writer.write(
            (
                "GET /authorize HTTP/1.1\r\n" + header.format(host=address.netloc) + "\r\n\r\n"
            ).encode()
        )
        await writer.drain()
        assert await reader.read() == b""
        writer.close()
        await writer.wait_closed()


@pytest.mark.parametrize("deadline", [0, 601, float("nan"), float("inf")])
async def test_invalid_timeout_never_opens_network(deadline: float) -> None:
    with pytest.raises(ValueError):
        await bootstrap.authorize(
            ProtectedOAuthTokenStorage(MemoryStore()), timeout_seconds=deadline
        )


@pytest.mark.parametrize("fault", ["pkce", "registration_supported", "token_failure"])
async def test_unsupported_server_or_failure_leaves_credentials_unchanged(
    fault: str, capsys: pytest.CaptureFixture[str], caplog: pytest.LogCaptureFixture
) -> None:
    fixture = OAuthServerFixture()
    setattr(fixture, fault, fault == "token_failure")
    store = MemoryStore()
    storage = ProtectedOAuthTokenStorage(store)
    await storage.set_tokens(OAuthToken(access_token="existing-credential", expires_in=30))
    before = deepcopy(store.records)
    with pytest.raises(AuthenticationRequiredError) as error:
        await bootstrap.authorize(
            storage,
            opener=automatic_fixture_browser,
            transport=httpx2.MockTransport(fixture.handle),
            timeout_seconds=10,
        )
    assert store.records == before
    output = str(error.value) + caplog.text + str(capsys.readouterr())
    assert "fixture-secret-must-not-escape" not in output
    assert "existing-credential" not in output
    assert (
        not any("/register" in str(request.url) for request in fixture.requests)
        if (fault in {"pkce", "registration_supported"})
        else True
    )


async def test_callback_timeout_closes_listener_without_persisting() -> None:
    fixture = OAuthServerFixture()
    store = MemoryStore()
    opened: list[str] = []

    async def opener(url: str) -> None:
        opened.append(url)

    prior_logging = logging.root.manager.disable
    with pytest.raises(AuthenticationRequiredError):
        await bootstrap.authorize(
            ProtectedOAuthTokenStorage(store),
            opener=opener,
            transport=httpx2.MockTransport(fixture.handle),
            timeout_seconds=1,
        )
    assert not store.records
    assert logging.root.manager.disable == prior_logging
    assert len(opened) == 1
    with pytest.raises(OSError):
        await asyncio.open_connection("127.0.0.1", urlsplit(opened[0]).port)


async def test_no_business_operation_can_cross_bootstrap_provider() -> None:
    async with bootstrap.LoopbackCallback() as callback:
        provider = bootstrap._BootstrapProvider(
            bootstrap._StagedStorage(ProtectedOAuthTokenStorage(MemoryStore()), None),
            callback,
            automatic_fixture_browser,
        )
        for method in ("tools/call", "tools/list", "get_accounts", "place_option_order"):
            with pytest.raises(AuthenticationRequiredError):
                provider._validate_request(
                    httpx2.Request(
                        "POST",
                        MCP_STREAMABLE_HTTP_ENDPOINT,
                        json={"jsonrpc": "2.0", "id": 1, "method": method},
                    )
                )


def test_cli_rejects_unattended_input_without_constructing_store(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(bootstrap.sys.stdin, "isatty", lambda: False)
    assert bootstrap.cli(["authorize"]) == 2
    assert "interactive native Windows" in capsys.readouterr().err
