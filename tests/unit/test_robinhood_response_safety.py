from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from mcp.types import CallToolResult

from app.broker.robinhood_mcp import (
    RobinhoodReadOnlyMcpClient,
    SelectedAgenticAccount,
    _extract_payload,
    _normalize_tool_list,
    _parse_account,
    _parse_review,
    _unwrap_sequence,
)
from app.clock.base import VirtualClock
from app.domain.enums import ExecutionEnvironment
from app.domain.models import TradeProposal
from app.exceptions import DataInvalidError, SafetyCriticalError

NOW = datetime(2026, 9, 3, 14, tzinfo=UTC)


def account_data() -> dict[str, Any]:
    return {
        "account_fingerprint": "selected-agentic-fingerprint",
        "cash": "0",
        "buying_power": "0",
        "as_of": NOW.isoformat(),
        "is_authenticated": True,
        "state_known": True,
    }


def parse_account(data: Any) -> Any:
    return _parse_account(data, clock=VirtualClock(NOW), environment=ExecutionEnvironment.DEMO)


@pytest.mark.parametrize(
    "missing",
    ["cash", "buying_power", "account_fingerprint", "as_of", "is_authenticated", "state_known"],
)
def test_account_requires_explicit_authoritative_fields(missing: str) -> None:
    data = account_data()
    del data[missing]
    with pytest.raises(DataInvalidError):
        parse_account(data)


@pytest.mark.parametrize("field", ["is_authenticated", "state_known"])
@pytest.mark.parametrize("value", ["true", "false", 1, 0, None])
def test_account_boolean_evidence_is_not_truthiness(field: str, value: Any) -> None:
    data = account_data() | {field: value}
    with pytest.raises(DataInvalidError, match="explicit boolean"):
        parse_account(data)


def test_explicit_unknown_or_unauthenticated_state_is_preserved() -> None:
    snapshot = parse_account(account_data() | {"is_authenticated": False, "state_known": False})
    assert not snapshot.is_authenticated
    assert not snapshot.state_known


@pytest.mark.parametrize("value", ["NaN", "Infinity", "-Infinity", "-1"])
def test_account_cash_must_be_finite_and_nonnegative(value: str) -> None:
    with pytest.raises(DataInvalidError):
        parse_account(account_data() | {"cash": value})


def test_account_timestamp_cannot_be_fabricated_or_future() -> None:
    for timestamp in (NOW.replace(tzinfo=None), NOW + timedelta(seconds=1)):
        with pytest.raises(DataInvalidError):
            parse_account(account_data() | {"as_of": timestamp})


@pytest.mark.parametrize("value", [{}, {"unexpected": []}, {"positions": None}, {"data": {}}])
def test_unrecognized_collection_is_not_an_empty_account(value: Any) -> None:
    with pytest.raises(DataInvalidError, match="recognized collection"):
        _unwrap_sequence(value, "positions")


def test_explicit_empty_collection_is_valid() -> None:
    assert _unwrap_sequence({"positions": []}, "positions") == ()
    assert _unwrap_sequence([], "positions") == ()


@pytest.mark.parametrize("flag", ["isError", "is_error"])
def test_error_results_are_never_successful_structured_payloads(flag: str) -> None:
    with pytest.raises(DataInvalidError, match="not authoritative"):
        _extract_payload({flag: True, "structuredContent": account_data()})


def test_installed_mcp_v2_error_result_is_rejected() -> None:
    result = CallToolResult(content=[], structured_content=account_data(), is_error=True)
    with pytest.raises(DataInvalidError, match="not authoritative"):
        parse_account(result)


def test_installed_mcp_v2_success_result_preserves_explicit_account_evidence() -> None:
    result = CallToolResult(content=[], structured_content=account_data(), is_error=False)
    assert parse_account(result).account_fingerprint == "selected-agentic-fingerprint"


def test_nested_errors_are_not_valid_account_or_review_evidence(
    proposal: TradeProposal,
) -> None:
    with pytest.raises(DataInvalidError, match="not authoritative"):
        parse_account({"account": account_data() | {"error": "upstream failed"}})
    with pytest.raises(DataInvalidError, match="not authoritative"):
        _parse_review(
            {"review": {"accepted": True, "error": "upstream failed"}},
            proposal=proposal,
            clock=VirtualClock(NOW),
        )


def test_content_envelope_with_metadata_is_not_mistaken_for_a_domain_object() -> None:
    assert _extract_payload(
        {"content": [{"type": "text", "text": '{"positions": []}'}], "isError": False, "_meta": {}}
    ) == {"positions": []}


@pytest.mark.parametrize("value", [{}, {"warnings": []}, {"accepted": "false"}, {"approved": 1}])
def test_review_requires_explicit_boolean_acceptance(value: Any, proposal: TradeProposal) -> None:
    with pytest.raises(DataInvalidError, match="explicit boolean"):
        _parse_review(value, proposal=proposal, clock=VirtualClock(NOW))


def test_negative_review_is_preserved_and_conflicting_status_is_rejected(
    proposal: TradeProposal,
) -> None:
    assert not _parse_review(
        {"accepted": False}, proposal=proposal, clock=VirtualClock(NOW)
    ).accepted
    with pytest.raises(DataInvalidError, match="conflicting"):
        _parse_review(
            {"accepted": True, "approved": False}, proposal=proposal, clock=VirtualClock(NOW)
        )


def test_partial_and_duplicate_tool_catalogs_fail_closed() -> None:
    tool = {"name": "get_accounts", "inputSchema": {"type": "object"}}
    with pytest.raises(DataInvalidError, match="pagination"):
        _normalize_tool_list({"tools": [tool], "nextCursor": "next-page"})
    with pytest.raises(DataInvalidError, match="duplicate"):
        _normalize_tool_list({"tools": [tool, tool]})


class OfficialNamesFixtureTransport:
    """Public tool names, deliberately synthetic private response schemas."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.portfolio = account_data()
        self.catalog: dict[str, Any] = {
            "tools": [
                {
                    "name": name,
                    "inputSchema": {
                        "type": "object",
                        "properties": {"selected_key": {"type": "string"}},
                        "required": ["selected_key"] if name == "get_portfolio" else [],
                        "additionalProperties": False,
                    },
                }
                for name in (
                    "get_accounts",
                    "get_portfolio",
                    "get_option_positions",
                    "get_option_orders",
                    "review_option_order",
                    "place_option_order",
                    "cancel_option_order",
                )
            ]
        }

    async def list_tools(self) -> Any:
        return self.catalog

    async def call_tool(self, name: str, arguments: Mapping[str, Any]) -> Any:
        self.calls.append((name, dict(arguments)))
        if name == "get_accounts":
            return {"fixture_accounts": ["user-selected-key"]}
        if name == "get_portfolio":
            return self.portfolio
        raise AssertionError("only selected-account reads are expected")


class FixtureAccountAdapter:
    expected_account_fingerprint = "selected-agentic-fingerprint"

    def __init__(self) -> None:
        self.selection = SelectedAgenticAccount(
            account_fingerprint=self.expected_account_fingerprint,
            agentic_allowed=True,
            request_context={"selected_key": "user-selected-key"},
        )

    def select_account(self, accounts_payload: Any) -> SelectedAgenticAccount:
        assert accounts_payload == {"fixture_accounts": ["user-selected-key"]}
        return self.selection

    def portfolio_arguments(
        self, account: SelectedAgenticAccount, input_schema: Mapping[str, Any]
    ) -> Mapping[str, Any]:
        return account.request_context

    def normalize_portfolio(
        self, account: SelectedAgenticAccount, portfolio_payload: Any
    ) -> Mapping[str, Any]:
        assert isinstance(portfolio_payload, Mapping)
        return portfolio_payload


async def test_official_account_tools_are_not_naive_snapshot_aliases() -> None:
    transport = OfficialNamesFixtureTransport()
    client = RobinhoodReadOnlyMcpClient(transport=transport, clock=VirtualClock(NOW))
    capabilities = await client.get_capabilities()
    assert capabilities.descriptor_for("get_accounts") is not None
    assert capabilities.descriptor_for("get_portfolio") is not None
    assert capabilities.descriptor_for("get_account_state") is None
    assert not capabilities.account_state
    assert not capabilities.execution_ready
    with pytest.raises(SafetyCriticalError, match="selected-account"):
        await client.get_account_state()
    assert transport.calls == []


async def test_trusted_account_adapter_binds_catalog_selection_and_portfolio() -> None:
    transport = OfficialNamesFixtureTransport()
    client = RobinhoodReadOnlyMcpClient(
        transport=transport,
        clock=VirtualClock(NOW),
        account_response_adapter=FixtureAccountAdapter(),
    )
    capabilities = await client.get_capabilities()
    assert capabilities.account_state and capabilities.execution_ready
    assert not capabilities.external_writes_enabled
    snapshot = await client.get_account_state()
    assert snapshot.account_fingerprint == "selected-agentic-fingerprint"
    assert transport.calls == [
        ("get_accounts", {}),
        ("get_portfolio", {"selected_key": "user-selected-key"}),
    ]


@pytest.mark.parametrize(
    "changes",
    [{"agentic_allowed": False}, {"account_fingerprint": "different-fingerprint"}],
)
async def test_non_agentic_or_wrong_selected_account_is_rejected(changes: dict[str, Any]) -> None:
    transport = OfficialNamesFixtureTransport()
    adapter = FixtureAccountAdapter()
    adapter.selection = adapter.selection.model_copy(update=changes)
    client = RobinhoodReadOnlyMcpClient(
        transport=transport, clock=VirtualClock(NOW), account_response_adapter=adapter
    )
    with pytest.raises(SafetyCriticalError, match="expected Agentic"):
        await client.get_account_state()
    assert [name for name, _ in transport.calls] == ["get_accounts"]


async def test_portfolio_mismatch_and_mid_session_selector_change_are_rejected() -> None:
    transport = OfficialNamesFixtureTransport()
    adapter = FixtureAccountAdapter()
    client = RobinhoodReadOnlyMcpClient(
        transport=transport, clock=VirtualClock(NOW), account_response_adapter=adapter
    )
    transport.portfolio["account_fingerprint"] = "wrong-account"
    with pytest.raises(SafetyCriticalError, match="different account"):
        await client.get_account_state()
    transport.portfolio = account_data()
    await client.get_account_state()
    adapter.selection = adapter.selection.model_copy(
        update={"request_context": {"selected_key": "another-account"}}
    )
    with pytest.raises(SafetyCriticalError, match="changed during"):
        await client.get_account_state()


async def test_read_tool_declaring_destructive_effects_is_unavailable() -> None:
    transport = OfficialNamesFixtureTransport()
    transport.catalog["tools"][0]["annotations"] = {"destructiveHint": True}
    client = RobinhoodReadOnlyMcpClient(
        transport=transport,
        clock=VirtualClock(NOW),
        account_response_adapter=FixtureAccountAdapter(),
    )
    assert not (await client.get_capabilities()).execution_ready
    with pytest.raises(SafetyCriticalError, match="unavailable or unsafe"):
        await client.get_account_state()
    assert transport.calls == []


async def test_in_place_selector_mutation_cannot_change_the_pinned_account() -> None:
    transport = OfficialNamesFixtureTransport()
    adapter = FixtureAccountAdapter()
    client = RobinhoodReadOnlyMcpClient(
        transport=transport, clock=VirtualClock(NOW), account_response_adapter=adapter
    )
    await client.get_account_state()
    adapter.selection.request_context["selected_key"] = "mutated-selector"
    with pytest.raises(SafetyCriticalError, match="changed during"):
        await client.get_account_state()


async def test_expected_account_identity_is_fixed_at_client_construction() -> None:
    transport = OfficialNamesFixtureTransport()
    adapter = FixtureAccountAdapter()
    client = RobinhoodReadOnlyMcpClient(
        transport=transport, clock=VirtualClock(NOW), account_response_adapter=adapter
    )
    adapter.expected_account_fingerprint = "unexpected-account"
    adapter.selection = adapter.selection.model_copy(
        update={"account_fingerprint": "unexpected-account"}
    )
    with pytest.raises(SafetyCriticalError, match="expected Agentic"):
        await client.get_account_state()
    assert [name for name, _ in transport.calls] == ["get_accounts"]
