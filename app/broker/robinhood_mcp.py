from __future__ import annotations

import asyncio
import hashlib
import json
from collections.abc import Mapping, Sequence
from datetime import UTC, date, datetime
from decimal import Decimal, InvalidOperation
from typing import Any, Protocol, runtime_checkable
from uuid import NAMESPACE_URL, UUID, uuid5

from pydantic import Field

from app.broker.base import (
    BrokerCapabilities,
    BrokerValueModel,
    CapabilityDescriptor,
    CommandIntentRecorder,
    IntentRecordingBroker,
    ReconciliationReport,
    validate_command_for_capability,
)
from app.clock.base import Clock
from app.domain.enums import (
    AccountKind,
    BrokerAction,
    ExecutionEnvironment,
    FirewallDisposition,
    OptionType,
    OrderSide,
    OrderState,
)
from app.domain.models import (
    AccountSnapshot,
    BrokerCommandIntent,
    BrokerOrder,
    BrokerReview,
    FirewallDecision,
    OptionContract,
    Position,
    TradeProposal,
    sha256_json,
)
from app.exceptions import (
    AuthenticationRequiredError,
    DataInvalidError,
    SafetyCriticalError,
    SubmissionUnknownError,
    TransientError,
)

MCP_STREAMABLE_HTTP_ENDPOINT = "https://agent.robinhood.com/mcp/trading"


@runtime_checkable
class McpV2Transport(Protocol):
    """Injectable MCP client surface compatible with current Python SDK results.

    An official-SDK session, or a thin OAuth/session wrapper around one, can
    implement this protocol.  Tests can inject an in-memory transport without
    brokerage credentials.  No SDK-private types leak across the broker API.
    """

    async def list_tools(self) -> Any:
        raise NotImplementedError

    async def call_tool(self, name: str, arguments: Mapping[str, Any]) -> Any:
        raise NotImplementedError


class LiveWriteAuthorizerProtocol(Protocol):
    async def evaluate(self, command: BrokerCommandIntent) -> FirewallDecision:
        raise NotImplementedError


class McpToolDefinition(BrokerValueModel):
    name: str = Field(min_length=1)
    description: str = ""
    input_schema: dict[str, Any] = Field(default_factory=dict)
    annotations: dict[str, Any] = Field(default_factory=dict)
    schema_version: str


class RobinhoodReadReviewClient(Protocol):
    """Structural shadow dependency: deliberately contains no generic/write call."""

    async def get_capabilities(self) -> BrokerCapabilities:
        raise NotImplementedError

    async def get_account_state(self) -> AccountSnapshot:
        raise NotImplementedError

    async def get_positions(self) -> tuple[Position, ...]:
        raise NotImplementedError

    async def get_orders(self) -> tuple[BrokerOrder, ...]:
        raise NotImplementedError

    async def review_option_order(self, proposal: TradeProposal) -> BrokerReview:
        raise NotImplementedError

    async def validate_command(self, command: BrokerCommandIntent) -> dict[str, Any]:
        raise NotImplementedError


_TOOL_ALIASES: dict[str, tuple[str, ...]] = {
    "get_account_state": (
        "get_account_state",
        "get_account",
        "get_account_details",
        "get_account_profile",
    ),
    "get_positions": ("get_option_positions", "get_positions", "list_option_positions"),
    "get_orders": ("get_option_orders", "get_orders", "list_option_orders"),
    "review_option_order": ("review_option_order",),
    "place_option_order": ("place_option_order",),
    "cancel_option_order": ("cancel_option_order",),
}

_READ_CAPABILITIES = frozenset({"get_account_state", "get_positions", "get_orders"})
_WRITE_CAPABILITIES = frozenset(
    {BrokerAction.PLACE_OPTION_ORDER.value, BrokerAction.CANCEL_OPTION_ORDER.value}
)
_REQUIRED_CAPABILITIES = tuple(_TOOL_ALIASES)


class RobinhoodReadOnlyMcpClient:
    """Allowlisted Robinhood MCP account/read/review facade for broker-shadow.

    The generic MCP transport is private to this facade.  The facade exposes no
    `call_tool`, placement, cancellation, or replacement operation.  Every
    network invocation is checked against a semantic read/review allowlist.
    Write tools are discovered only so their exact schemas can be recorded and
    validated locally.
    """

    adapter_version = "robinhood-read-review-mcp-v1"

    def __init__(
        self,
        *,
        transport: McpV2Transport,
        clock: Clock,
        environment: ExecutionEnvironment = ExecutionEnvironment.DEMO,
        safe_review_tools: frozenset[str] = frozenset({"review_option_order"}),
        expected_schema_hashes: Mapping[str, str] | None = None,
    ) -> None:
        self.__transport = transport
        self._clock = clock
        self._environment = environment
        self._safe_review_tools = safe_review_tools
        self._expected_schema_hashes = dict(expected_schema_hashes or {})
        self._capabilities: BrokerCapabilities | None = None
        self._tools_by_capability: dict[str, McpToolDefinition] = {}

    async def get_capabilities(self) -> BrokerCapabilities:
        if self._capabilities is None:
            await self.discover_capabilities()
        assert self._capabilities is not None
        return self._capabilities

    async def discover_capabilities(self) -> BrokerCapabilities:
        raw = await self.__transport.list_tools()
        tools = _normalize_tool_list(raw)
        by_name = {tool.name: tool for tool in tools}
        selected: dict[str, McpToolDefinition] = {}
        descriptors: list[CapabilityDescriptor] = []
        issues: list[str] = []

        for capability, aliases in _TOOL_ALIASES.items():
            tool = next((by_name[name] for name in aliases if name in by_name), None)
            if tool is None:
                issues.append(f"required Robinhood capability missing: {capability}")
                continue
            selected[capability] = tool
            side_effect_free = capability in _READ_CAPABILITIES
            if capability == "review_option_order":
                side_effect_free = tool.name in self._safe_review_tools and not bool(
                    tool.annotations.get("destructiveHint", False)
                )
                if not side_effect_free:
                    issues.append("review_option_order is not verified side-effect-free")
            descriptor = CapabilityDescriptor.from_schema(
                capability=capability,
                tool_name=tool.name,
                schema_version=tool.schema_version,
                input_schema=tool.input_schema,
                side_effect_free=side_effect_free,
                annotations=tool.annotations,
            )
            descriptors.append(descriptor)
            expected = self._expected_schema_hashes.get(capability)
            if expected is None:
                expected = self._expected_schema_hashes.get(tool.name)
            if expected is not None and expected != descriptor.schema_hash:
                issues.append(f"schema drift detected for {capability}")

        self._tools_by_capability = selected
        available = set(selected)
        execution_requirements = set(_REQUIRED_CAPABILITIES)
        self._capabilities = BrokerCapabilities(
            adapter_name="RobinhoodReadOnlyMcpClient",
            adapter_version=self.adapter_version,
            discovered_at=self._clock.now(),
            descriptors=tuple(descriptors),
            account_state="get_account_state" in available,
            positions="get_positions" in available,
            orders="get_orders" in available,
            review_option_orders=(
                "review_option_order" in available
                and next(
                    descriptor.side_effect_free
                    for descriptor in descriptors
                    if descriptor.capability == "review_option_order"
                )
            ),
            # These mean schemas are discoverable/locally modellable.  The
            # separate external_writes_enabled bit remains unconditionally false.
            place_option_orders="place_option_order" in available,
            cancel_option_orders="cancel_option_order" in available,
            reconcile=True,
            external_writes_enabled=False,
            execution_ready=execution_requirements.issubset(available) and not issues,
            issues=tuple(issues),
        )
        return self._capabilities

    async def get_account_state(self) -> AccountSnapshot:
        payload = await self._call_allowed("get_account_state", {})
        return _parse_account(payload, clock=self._clock, environment=self._environment)

    async def get_positions(self) -> tuple[Position, ...]:
        payload = await self._call_allowed("get_positions", {})
        return _parse_positions(payload, clock=self._clock, environment=self._environment)

    async def get_orders(self) -> tuple[BrokerOrder, ...]:
        payload = await self._call_allowed("get_orders", {})
        return _parse_orders(payload, clock=self._clock, environment=self._environment)

    async def review_option_order(self, proposal: TradeProposal) -> BrokerReview:
        capabilities = await self.get_capabilities()
        descriptor = capabilities.descriptor_for("review_option_order")
        if descriptor is None or not descriptor.side_effect_free:
            raise SafetyCriticalError("Robinhood review is not verified side-effect-free")
        arguments = _proposal_arguments(proposal, descriptor.input_schema)
        validated = descriptor.validate(arguments)
        payload = await self._call_allowed("review_option_order", validated)
        return _parse_review(payload, proposal=proposal, clock=self._clock)

    async def validate_command(self, command: BrokerCommandIntent) -> dict[str, Any]:
        capabilities = await self.get_capabilities()
        return validate_command_for_capability(command, capabilities)

    async def _call_allowed(self, capability: str, arguments: Mapping[str, Any]) -> Any:
        if capability not in _READ_CAPABILITIES and capability != "review_option_order":
            raise SafetyCriticalError(
                f"MCP capability is not read/review allowlisted: {capability}"
            )
        capabilities = await self.get_capabilities()
        descriptor = capabilities.descriptor_for(capability)
        if descriptor is None or not descriptor.side_effect_free:
            raise SafetyCriticalError(f"MCP capability is unavailable or unsafe: {capability}")
        if (
            capability == "review_option_order"
            and descriptor.tool_name not in self._safe_review_tools
        ):
            raise SafetyCriticalError("MCP review tool is outside the explicit allowlist")
        write_tool_names = {name for item in _WRITE_CAPABILITIES for name in _TOOL_ALIASES[item]}
        if descriptor.tool_name in write_tool_names:
            raise SafetyCriticalError("write tool reached read-only MCP facade")
        try:
            return await self.__transport.call_tool(descriptor.tool_name, dict(arguments))
        except (PermissionError, AuthenticationError) as exc:
            raise AuthenticationRequiredError("Robinhood MCP authentication is required") from exc
        except (TimeoutError, ConnectionError, OSError) as exc:
            raise TransientError(f"Robinhood MCP {capability} failed transiently") from exc


class RobinhoodMcpBroker(IntentRecordingBroker):
    """Live adapter skeleton with an injectable MCP v2-style transport.

    This is the only broker implementation containing a write transport.  It
    requires a separate live authorizer and calls only the exact validated
    argument mapping already persisted in ``BrokerCommandIntent``.
    """

    adapter_version = "robinhood-live-mcp-v1"

    def __init__(
        self,
        *,
        transport: McpV2Transport,
        clock: Clock,
        write_authorizer: LiveWriteAuthorizerProtocol,
        command_recorder: CommandIntentRecorder | None = None,
        safe_review_tools: frozenset[str] = frozenset({"review_option_order"}),
        expected_schema_hashes: Mapping[str, str] | None = None,
    ) -> None:
        super().__init__(command_recorder=command_recorder)
        self._clock = clock
        self._write_transport = transport
        self._write_authorizer = write_authorizer
        self._read_client = RobinhoodReadOnlyMcpClient(
            transport=transport,
            clock=clock,
            environment=ExecutionEnvironment.LIVE,
            safe_review_tools=safe_review_tools,
            expected_schema_hashes=expected_schema_hashes,
        )
        self._capabilities: BrokerCapabilities | None = None
        self._local_orders: dict[UUID, BrokerOrder] = {}
        self._write_results: dict[str, UUID] = {}

    async def get_capabilities(self) -> BrokerCapabilities:
        discovered = await self._read_client.get_capabilities()
        if self._capabilities is None:
            write_ready = (
                discovered.place_option_orders
                and discovered.cancel_option_orders
                and discovered.execution_ready
            )
            self._capabilities = discovered.model_copy(
                update={
                    "adapter_name": "RobinhoodMcpBroker",
                    "adapter_version": self.adapter_version,
                    "external_writes_enabled": write_ready,
                    "execution_ready": write_ready,
                }
            )
        return self._capabilities

    async def get_observed_broker_account_state(self) -> AccountSnapshot:
        return await self._read_client.get_account_state()

    async def get_effective_execution_account_state(self) -> AccountSnapshot:
        return await self.get_observed_broker_account_state()

    async def get_positions(self) -> tuple[Position, ...]:
        return await self._read_client.get_positions()

    async def get_orders(self) -> tuple[BrokerOrder, ...]:
        remote = await self._read_client.get_orders()
        by_broker_id = {order.broker_order_id for order in remote if order.broker_order_id}
        unresolved = tuple(
            order
            for order in self._local_orders.values()
            if order.broker_order_id not in by_broker_id
            and order.state in {OrderState.SUBMITTING, OrderState.SUBMISSION_UNKNOWN}
        )
        return remote + unresolved

    async def review_option_order(self, proposal: TradeProposal) -> BrokerReview:
        if proposal.environment is not ExecutionEnvironment.LIVE:
            raise SafetyCriticalError("live broker received non-LIVE proposal")
        return await self._read_client.review_option_order(proposal)

    async def place_option_order(
        self,
        command: BrokerCommandIntent,
        contract: OptionContract,
    ) -> BrokerOrder:
        if command.environment is not ExecutionEnvironment.LIVE:
            raise SafetyCriticalError("live broker received non-LIVE command")
        if command.action is not BrokerAction.PLACE_OPTION_ORDER:
            raise DataInvalidError("placement requires PLACE_OPTION_ORDER command")
        if command.instrument_id != contract.instrument_id:
            raise DataInvalidError("command instrument and contract differ")

        duplicate = self._existing_write_result(command)
        if duplicate is not None:
            if duplicate.state is OrderState.SUBMISSION_UNKNOWN:
                raise SubmissionUnknownError(
                    "submission outcome is unknown; reconcile instead of retrying"
                )
            return duplicate

        validated = await self._read_client.validate_command(command)
        await self.record_broker_command_intent(command)
        now = self._clock.now()
        local = BrokerOrder(
            order_id=uuid5(NAMESPACE_URL, f"options-sentinel-live:{command.order_intent_id}"),
            broker_order_id=None,
            intent_id=command.order_intent_id,
            environment=ExecutionEnvironment.LIVE,
            state=OrderState.SUBMITTING,
            contract=contract,
            side=command.side,
            quantity=command.quantity,
            filled_quantity=0,
            limit_price=command.limit_price,
            average_fill_price=None,
            submitted_at=now,
            created_at=now,
        )
        self._local_orders[local.order_id] = local
        self._write_results[command.idempotency_key] = local.order_id

        authorization = await self._write_authorizer.evaluate(command)
        _validate_authorization(authorization, command)
        if (
            authorization.disposition is not FirewallDisposition.AUTHORIZED_LIVE
            or authorization.environment is not ExecutionEnvironment.LIVE
        ):
            rejected = local.model_copy(update={"state": OrderState.REJECTED})
            self._local_orders[local.order_id] = rejected
            return rejected

        try:
            raw = await self._write_transport.call_tool(command.capability_name, validated)
        except (TimeoutError, ConnectionError, OSError) as exc:
            unknown = local.model_copy(update={"state": OrderState.SUBMISSION_UNKNOWN})
            self._local_orders[local.order_id] = unknown
            raise SubmissionUnknownError(
                "broker may have accepted the order; reconciliation is mandatory"
            ) from exc
        parsed = _parse_order_ack(raw, fallback=local)
        self._local_orders[local.order_id] = parsed
        return parsed

    async def cancel_option_order(
        self,
        command: BrokerCommandIntent,
        order_id: UUID | str,
    ) -> BrokerOrder:
        if command.environment is not ExecutionEnvironment.LIVE:
            raise SafetyCriticalError("live broker received non-LIVE command")
        if command.action is not BrokerAction.CANCEL_OPTION_ORDER:
            raise DataInvalidError("cancellation requires CANCEL_OPTION_ORDER command")
        duplicate = self._existing_write_result(command)
        if duplicate is not None:
            if duplicate.state is OrderState.SUBMISSION_UNKNOWN:
                raise SubmissionUnknownError(
                    "cancellation outcome is unknown; reconcile instead of retrying"
                )
            return duplicate

        validated = await self._read_client.validate_command(command)
        _validate_cancel_target(validated, order_id)
        await self.record_broker_command_intent(command)
        target = self._find_local_order(order_id)
        if target is None:
            remote_orders = await self._read_client.get_orders()
            target = next(
                (
                    order
                    for order in remote_orders
                    if order.order_id == order_id or order.broker_order_id == str(order_id)
                ),
                None,
            )
        if target is None:
            raise DataInvalidError(f"cannot cancel unknown broker order: {order_id}")
        pending = target.model_copy(update={"state": OrderState.SUBMITTING})
        self._local_orders[pending.order_id] = pending
        self._write_results[command.idempotency_key] = pending.order_id

        authorization = await self._write_authorizer.evaluate(command)
        _validate_authorization(authorization, command)
        if authorization.disposition is not FirewallDisposition.AUTHORIZED_LIVE:
            return target
        try:
            raw = await self._write_transport.call_tool(command.capability_name, validated)
        except (TimeoutError, ConnectionError, OSError) as exc:
            unknown = pending.model_copy(update={"state": OrderState.SUBMISSION_UNKNOWN})
            self._local_orders[pending.order_id] = unknown
            raise SubmissionUnknownError(
                "broker may have received cancellation; reconciliation is mandatory"
            ) from exc
        parsed = _parse_order_ack(raw, fallback=pending)
        self._local_orders[pending.order_id] = parsed
        return parsed

    async def reconcile(self) -> ReconciliationReport:
        observed, positions, remote_orders = await asyncio.gather(
            self.get_observed_broker_account_state(),
            self.get_positions(),
            self._read_client.get_orders(),
        )
        discrepancies: list[str] = []
        remote_ids = {order.broker_order_id for order in remote_orders if order.broker_order_id}
        for local in self._local_orders.values():
            if local.state is not OrderState.SUBMISSION_UNKNOWN:
                continue
            if local.broker_order_id is None or local.broker_order_id not in remote_ids:
                discrepancies.append(f"SUBMISSION_UNKNOWN unresolved: {local.order_id}")
        return ReconciliationReport(
            environment=ExecutionEnvironment.LIVE,
            reconciled_at=self._clock.now(),
            successful=not discrepancies,
            observed_account=observed,
            effective_account=observed,
            position_count=len(positions),
            open_order_count=sum(
                order.state in {OrderState.OPEN, OrderState.PARTIAL, OrderState.SUBMITTING}
                for order in remote_orders
            ),
            discrepancies=tuple(discrepancies),
            details={"source": "authoritative Robinhood MCP reads"},
        )

    def _existing_write_result(self, command: BrokerCommandIntent) -> BrokerOrder | None:
        order_id = self._write_results.get(command.idempotency_key)
        if order_id is None:
            return None
        prior_command_id = next(
            (
                item.command_intent_id
                for item in self.recorded_command_intents
                if item.idempotency_key == command.idempotency_key
            ),
            None,
        )
        if prior_command_id is not None and prior_command_id != command.command_intent_id:
            raise SafetyCriticalError("live idempotency key was reused for another command")
        return self._local_orders[order_id]

    def _find_local_order(self, order_id: UUID | str) -> BrokerOrder | None:
        if isinstance(order_id, UUID):
            return self._local_orders.get(order_id)
        try:
            parsed = UUID(order_id)
        except ValueError:
            parsed = None
        if parsed is not None and parsed in self._local_orders:
            return self._local_orders[parsed]
        return next(
            (order for order in self._local_orders.values() if order.broker_order_id == order_id),
            None,
        )


# Alias used by configuration/docs.
RobinhoodLiveBroker = RobinhoodMcpBroker


class AuthenticationError(Exception):
    """Transport wrappers may translate SDK authentication failures to this type."""


def _normalize_tool_list(raw: Any) -> tuple[McpToolDefinition, ...]:
    if hasattr(raw, "tools"):
        raw = raw.tools
    elif isinstance(raw, Mapping) and "tools" in raw:
        raw = raw["tools"]
    if not isinstance(raw, Sequence) or isinstance(raw, (str, bytes, bytearray)):
        raise DataInvalidError("MCP list_tools response does not contain a tool sequence")

    tools: list[McpToolDefinition] = []
    for item in raw:
        mapping = _object_mapping(item)
        name = mapping.get("name")
        if not isinstance(name, str) or not name:
            raise DataInvalidError("MCP tool definition is missing its name")
        schema_raw = mapping.get("inputSchema", mapping.get("input_schema", {}))
        if not isinstance(schema_raw, Mapping):
            raise DataInvalidError(f"MCP tool {name} has a non-object input schema")
        schema = dict(schema_raw)
        annotations_raw = mapping.get("annotations", {}) or {}
        annotations = (
            dict(annotations_raw)
            if isinstance(annotations_raw, Mapping)
            else _object_mapping(annotations_raw)
        )
        meta = mapping.get("_meta", mapping.get("meta", {})) or {}
        meta_mapping = dict(meta) if isinstance(meta, Mapping) else _object_mapping(meta)
        explicit_version = (
            mapping.get("schemaVersion")
            or mapping.get("schema_version")
            or meta_mapping.get("schemaVersion")
            or meta_mapping.get("version")
        )
        schema_version = str(explicit_version or f"mcp-schema-{sha256_json(schema)[:16]}")
        tools.append(
            McpToolDefinition(
                name=name,
                description=str(mapping.get("description", "") or ""),
                input_schema=schema,
                annotations=annotations,
                schema_version=schema_version,
            )
        )
    return tuple(tools)


def _object_mapping(value: Any) -> dict[str, Any]:
    if isinstance(value, Mapping):
        return dict(value)
    if hasattr(value, "model_dump"):
        dumped = value.model_dump(by_alias=True)
        if isinstance(dumped, Mapping):
            return dict(dumped)
    if hasattr(value, "__dict__"):
        return {key: item for key, item in vars(value).items() if not key.startswith("_")}
    raise DataInvalidError(f"expected object-like MCP value, got {type(value)!r}")


def _extract_payload(raw: Any) -> Any:
    """Normalize SDK CallToolResult, JSON mappings, or text content."""

    if hasattr(raw, "structuredContent") and raw.structuredContent is not None:
        return raw.structuredContent
    if hasattr(raw, "structured_content") and raw.structured_content is not None:
        return raw.structured_content
    if isinstance(raw, Mapping):
        if raw.get("structuredContent") is not None:
            return raw["structuredContent"]
        if raw.get("structured_content") is not None:
            return raw["structured_content"]
        # Plain mock transports commonly return the domain payload directly.
        if "content" not in raw or len(raw) > 2:
            return raw
        content = raw.get("content")
    else:
        content = getattr(raw, "content", None)

    if isinstance(content, Sequence) and not isinstance(content, (str, bytes, bytearray)):
        text_parts: list[str] = []
        for block in content:
            if isinstance(block, Mapping):
                text = block.get("text")
            else:
                text = getattr(block, "text", None)
            if isinstance(text, str):
                text_parts.append(text)
        joined = "\n".join(text_parts).strip()
        if not joined:
            return {}
        try:
            return json.loads(joined)
        except json.JSONDecodeError:
            return {"text": joined}
    return raw


def _unwrap_mapping(payload: Any, *keys: str) -> dict[str, Any]:
    value = _extract_payload(payload)
    if not isinstance(value, Mapping):
        raise DataInvalidError("MCP result payload is not an object")
    mapping = dict(value)
    for key in keys:
        nested = mapping.get(key)
        if isinstance(nested, Mapping):
            return dict(nested)
    for key in ("data", "result"):
        nested = mapping.get(key)
        if isinstance(nested, Mapping):
            return dict(nested)
    return mapping


def _unwrap_sequence(payload: Any, *keys: str) -> tuple[Any, ...]:
    value = _extract_payload(payload)
    if isinstance(value, Mapping):
        for key in (*keys, "data", "result"):
            nested = value.get(key)
            if isinstance(nested, Sequence) and not isinstance(nested, (str, bytes, bytearray)):
                return tuple(nested)
        return ()
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return tuple(value)
    raise DataInvalidError("MCP result payload does not contain a sequence")


def _proposal_arguments(proposal: TradeProposal, schema: Mapping[str, Any]) -> dict[str, Any]:
    side = proposal.side.value
    candidates: dict[str, Any] = {
        "instrument_id": proposal.contract.instrument_id,
        "option_instrument_id": proposal.contract.instrument_id,
        "option_id": proposal.contract.instrument_id,
        "symbol": proposal.symbol,
        "option_symbol": proposal.symbol,
        "side": side,
        "quantity": proposal.quantity,
        "limit_price": str(proposal.limit_price),
        "price": str(proposal.limit_price),
        "time_in_force": "day",
        "option_type": proposal.contract.option_type.value,
        "expiration": proposal.contract.expiration.isoformat(),
        "strike": str(proposal.contract.strike),
    }
    properties = schema.get("properties", {})
    if not isinstance(properties, Mapping) or not properties:
        return {
            "instrument_id": proposal.contract.instrument_id,
            "side": side,
            "quantity": proposal.quantity,
            "limit_price": str(proposal.limit_price),
            "time_in_force": "day",
        }

    arguments: dict[str, Any] = {}
    for name, field_schema in properties.items():
        if name not in candidates:
            continue
        value = candidates[name]
        if name == "side" and isinstance(field_schema, Mapping):
            allowed = field_schema.get("enum")
            if isinstance(allowed, Sequence) and side not in allowed:
                short_side = "buy" if proposal.side is OrderSide.BUY_TO_OPEN else "sell"
                if short_side in allowed:
                    value = short_side
        arguments[name] = value
    missing = set(schema.get("required", ())).difference(arguments)
    if missing:
        raise DataInvalidError(
            f"cannot construct Robinhood review arguments for required fields: {sorted(missing)!r}"
        )
    return arguments


def _parse_account(
    payload: Any,
    *,
    clock: Clock,
    environment: ExecutionEnvironment,
) -> AccountSnapshot:
    data = _unwrap_mapping(payload, "account", "account_state", "profile")
    now = clock.now()
    cash = _decimal_from(data, "cash", "cash_balance", "withdrawable_cash", default="0")
    buying_power = _decimal_from(
        data,
        "option_buying_power",
        "buying_power",
        "cash_available_for_withdrawal",
        default=str(cash),
    )
    if cash < 0 or buying_power < 0:
        raise DataInvalidError("broker account returned negative cash/buying power")
    raw_account_id = _first(data, "account_fingerprint", "account_id", "account_number", "id")
    fingerprint: str | None
    if raw_account_id is None:
        fingerprint = None
    elif "account_fingerprint" in data:
        fingerprint = str(raw_account_id)
    else:
        digest = hashlib.sha256(str(raw_account_id).encode()).hexdigest()
        fingerprint = f"sha256:{digest}"
    as_of = _datetime_value(
        _first(data, "as_of", "updated_at", "timestamp"),
        default=now,
    )
    return AccountSnapshot(
        created_at=now,
        environment=environment,
        account_kind=AccountKind.BROKER_OBSERVED,
        account_fingerprint=fingerprint,
        cash=cash,
        buying_power=buying_power,
        open_option_risk=_decimal_from(data, "open_option_risk", default="0"),
        open_positions=_int_from(data, "open_positions", "open_position_count", default=0),
        new_entries_today=_int_from(data, "new_entries_today", default=0),
        as_of=as_of,
        is_authenticated=bool(data.get("is_authenticated", True)),
        state_known=bool(data.get("state_known", True)),
    )


def _parse_positions(
    payload: Any,
    *,
    clock: Clock,
    environment: ExecutionEnvironment,
) -> tuple[Position, ...]:
    raw_items = _unwrap_sequence(payload, "positions", "option_positions")
    parsed: list[Position] = []
    for raw in raw_items:
        if isinstance(raw, Position):
            parsed.append(raw)
            continue
        data = _object_mapping(raw)
        try:
            parsed.append(Position.model_validate(data))
            continue
        except (ValueError, TypeError):
            pass
        contract = _parse_contract(data)
        quantity = _int_from(data, "quantity", "open_quantity", default=0)
        if quantity <= 0:
            continue
        average = _decimal_from(data, "average_entry_price", "average_price", "cost", default="0")
        if average <= 0:
            raise DataInvalidError("broker position lacks a positive average entry price")
        bid = _decimal_from(data, "current_bid", "bid", default="0")
        ask = _decimal_from(data, "current_ask", "ask", default=str(bid))
        identity = str(_first(data, "position_id", "id") or contract.instrument_id)
        created = _datetime_value(_first(data, "created_at", "opened_at"), default=clock.now())
        parsed.append(
            Position(
                created_at=created,
                environment=environment,
                contract=contract,
                quantity=quantity,
                average_entry_price=average,
                current_bid=bid,
                current_ask=ask,
                realized_pnl=_decimal_from(data, "realized_pnl", default="0"),
                best_unrealized_pnl=_decimal_from(data, "best_unrealized_pnl", default="0"),
                worst_unrealized_pnl=_decimal_from(data, "worst_unrealized_pnl", default="0"),
                thesis_id=_uuid_value(
                    _first(data, "thesis_id"),
                    fallback=f"robinhood-position:{identity}",
                ),
                invalidation_conditions=tuple(data.get("invalidation_conditions", ())),
                exit_policy_version=str(data.get("exit_policy_version", "broker-observed")),
            )
        )
    return tuple(parsed)


def _parse_orders(
    payload: Any,
    *,
    clock: Clock,
    environment: ExecutionEnvironment,
) -> tuple[BrokerOrder, ...]:
    raw_items = _unwrap_sequence(payload, "orders", "option_orders")
    return tuple(
        _parse_order_mapping(item, clock=clock, environment=environment) for item in raw_items
    )


def _parse_order_mapping(
    raw: Any,
    *,
    clock: Clock,
    environment: ExecutionEnvironment,
) -> BrokerOrder:
    if isinstance(raw, BrokerOrder):
        return raw
    data = _object_mapping(raw)
    contract = _parse_contract(data)
    broker_order_id = str(_first(data, "broker_order_id", "order_id", "id") or "") or None
    stable_identity = broker_order_id or sha256_json(data)
    quantity = _int_from(data, "quantity", default=1)
    filled = _int_from(data, "filled_quantity", "cumulative_quantity", default=0)
    limit_price = _decimal_from(data, "limit_price", "price", default="0")
    if limit_price <= 0:
        raise DataInvalidError("broker order lacks a positive limit price")
    average_raw = _first(data, "average_fill_price", "average_price")
    average = _to_decimal(average_raw) if average_raw not in (None, "") else None
    created = _datetime_value(_first(data, "created_at", "submitted_at"), default=clock.now())
    submitted_raw = _first(data, "submitted_at", "created_at")
    submitted = _datetime_value(submitted_raw, default=created) if submitted_raw else None
    return BrokerOrder(
        order_id=_uuid_value(
            _first(data, "local_order_id"), fallback=f"robinhood-order:{stable_identity}"
        ),
        broker_order_id=broker_order_id,
        intent_id=_uuid_value(
            _first(data, "intent_id"), fallback=f"robinhood-intent:{stable_identity}"
        ),
        environment=environment,
        state=_order_state(_first(data, "state", "status") or "OPEN"),
        contract=contract,
        side=_order_side(_first(data, "side", "direction") or "buy_to_open"),
        quantity=quantity,
        filled_quantity=filled,
        limit_price=limit_price,
        average_fill_price=average,
        submitted_at=submitted,
        created_at=created,
    )


def _parse_contract(data: Mapping[str, Any]) -> OptionContract:
    nested = data.get("contract") or data.get("option") or data.get("instrument")
    source = dict(nested) if isinstance(nested, Mapping) else dict(data)
    try:
        return OptionContract.model_validate(source)
    except (ValueError, TypeError):
        pass
    instrument_id = _first(
        source, "instrument_id", "option_instrument_id", "option_id", "id", "url"
    )
    symbol = _first(source, "symbol", "chain_symbol", "underlying_symbol")
    expiration_raw = _first(source, "expiration", "expiration_date")
    strike = _decimal_from(source, "strike", "strike_price", default="0")
    if not instrument_id or not symbol or expiration_raw is None or strike <= 0:
        raise DataInvalidError("broker payload lacks a complete option contract")
    option_raw = str(_first(source, "option_type", "type") or "").lower()
    try:
        option_type = OptionType(option_raw)
    except ValueError as exc:
        raise DataInvalidError(f"unknown option type: {option_raw!r}") from exc
    expiration = (
        expiration_raw
        if isinstance(expiration_raw, date)
        else date.fromisoformat(str(expiration_raw)[:10])
    )
    return OptionContract(
        instrument_id=str(instrument_id),
        symbol=str(symbol),
        option_type=option_type,
        strike=strike,
        expiration=expiration,
        multiplier=_int_from(source, "multiplier", default=100),
    )


def _parse_review(payload: Any, *, proposal: TradeProposal, clock: Clock) -> BrokerReview:
    data = _unwrap_mapping(payload, "review", "order_review")
    accepted_raw = _first(data, "accepted", "approved", "is_valid", "can_submit")
    alerts = data.get("warnings", data.get("alerts", ())) or ()
    warnings: tuple[str, ...]
    if isinstance(alerts, str):
        warnings = (alerts,)
    elif isinstance(alerts, Sequence):
        warnings = tuple(
            str(item.get("message", item)) if isinstance(item, Mapping) else str(item)
            for item in alerts
        )
    else:
        warnings = (str(alerts),)
    accepted = bool(accepted_raw) if accepted_raw is not None else not bool(data.get("error"))
    reference = _first(data, "review_id", "reference_id", "id")
    return BrokerReview(
        created_at=clock.now(),
        environment=proposal.environment,
        proposal_id=proposal.proposal_id,
        accepted=accepted,
        warnings=warnings,
        raw_reference=str(reference) if reference is not None else None,
        side_effect_free=True,
    )


def _parse_order_ack(raw: Any, *, fallback: BrokerOrder) -> BrokerOrder:
    data = _unwrap_mapping(raw, "order", "option_order")
    broker_order_id = _first(data, "broker_order_id", "order_id", "id")
    state_raw = _first(data, "state", "status")
    # An HTTP/tool acknowledgement without authoritative order state remains
    # SUBMITTING.  It is never promoted to OPEN/FILLED merely because a call returned.
    state = _order_state(state_raw) if state_raw is not None else OrderState.SUBMITTING
    filled = _int_from(data, "filled_quantity", "cumulative_quantity", default=0)
    average_raw = _first(data, "average_fill_price", "average_price")
    average = _to_decimal(average_raw) if average_raw not in (None, "") else None
    return fallback.model_copy(
        update={
            "broker_order_id": str(broker_order_id) if broker_order_id is not None else None,
            "state": state,
            "filled_quantity": filled,
            "average_fill_price": average,
        }
    )


def _validate_command_argument_consistency(
    command: BrokerCommandIntent,
    arguments: Mapping[str, Any],
) -> None:
    instrument = _first(
        arguments, "instrument_id", "option_instrument_id", "option_id", "instrument"
    )
    if instrument is not None and str(instrument) != command.instrument_id:
        raise SafetyCriticalError("validated MCP arguments contain a different instrument")
    quantity = _first(arguments, "quantity", "qty")
    if quantity is not None and int(quantity) != command.quantity:
        raise SafetyCriticalError("validated MCP arguments contain a different quantity")
    price = _first(arguments, "limit_price", "price")
    if price is not None and _to_decimal(price) != command.limit_price:
        raise SafetyCriticalError("validated MCP arguments contain a different limit price")
    side = _first(arguments, "side")
    if side is not None:
        accepted = {command.side.value}
        accepted.add("buy" if command.side is OrderSide.BUY_TO_OPEN else "sell")
        if str(side).lower() not in accepted:
            raise SafetyCriticalError("validated MCP arguments contain a different order side")


def _validate_cancel_target(arguments: Mapping[str, Any], order_id: UUID | str) -> None:
    target = _first(arguments, "order_id", "broker_order_id", "id")
    if target is not None and str(target) != str(order_id):
        raise SafetyCriticalError("cancel command arguments target a different order")


def _validate_authorization(
    decision: FirewallDecision,
    command: BrokerCommandIntent,
) -> None:
    if decision.command_intent_id != command.command_intent_id:
        raise SafetyCriticalError("live authorizer returned a decision for another command")
    if decision.environment is not ExecutionEnvironment.LIVE:
        raise SafetyCriticalError("live authorizer returned a non-LIVE decision")
    if decision.transmitted:
        raise SafetyCriticalError("authorizer cannot claim transport occurred before broker call")


def _first(mapping: Mapping[str, Any], *keys: str) -> Any:
    for key in keys:
        if key in mapping and mapping[key] is not None:
            return mapping[key]
    return None


def _to_decimal(value: Any) -> Decimal:
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise DataInvalidError(f"invalid broker decimal: {value!r}") from exc


def _decimal_from(
    mapping: Mapping[str, Any],
    *keys: str,
    default: str,
) -> Decimal:
    value = _first(mapping, *keys)
    return _to_decimal(default if value is None else value)


def _int_from(mapping: Mapping[str, Any], *keys: str, default: int) -> int:
    value = _first(mapping, *keys)
    try:
        return int(default if value is None else value)
    except (TypeError, ValueError) as exc:
        raise DataInvalidError(f"invalid broker integer: {value!r}") from exc


def _datetime_value(value: Any, *, default: datetime) -> datetime:
    if value is None:
        result = default
    elif isinstance(value, datetime):
        result = value
    else:
        raw = str(value).replace("Z", "+00:00")
        try:
            result = datetime.fromisoformat(raw)
        except ValueError as exc:
            raise DataInvalidError(f"invalid broker timestamp: {value!r}") from exc
    if result.tzinfo is None or result.utcoffset() is None:
        raise DataInvalidError("broker timestamp must be timezone-aware")
    return result.astimezone(UTC)


def _uuid_value(value: Any, *, fallback: str) -> UUID:
    if isinstance(value, UUID):
        return value
    if value is not None:
        try:
            return UUID(str(value))
        except ValueError:
            pass
    return uuid5(NAMESPACE_URL, fallback)


def _order_state(value: Any) -> OrderState:
    normalized = str(value).strip().upper().replace("-", "_").replace(" ", "_")
    aliases = {
        "QUEUED": OrderState.OPEN,
        "PENDING": OrderState.OPEN,
        "CONFIRMED": OrderState.OPEN,
        "UNCONFIRMED": OrderState.SUBMITTING,
        "PARTIALLY_FILLED": OrderState.PARTIAL,
        "PARTIAL_FILL": OrderState.PARTIAL,
        "CANCELLED": OrderState.CANCELED,
        "VOIDED": OrderState.CANCELED,
        "FAILED": OrderState.REJECTED,
    }
    if normalized in aliases:
        return aliases[normalized]
    try:
        return OrderState(normalized)
    except ValueError as exc:
        raise DataInvalidError(f"unknown broker order state: {value!r}") from exc


def _order_side(value: Any) -> OrderSide:
    normalized = str(value).strip().lower().replace("-", "_").replace(" ", "_")
    aliases = {
        "buy": OrderSide.BUY_TO_OPEN,
        "open": OrderSide.BUY_TO_OPEN,
        "buy_to_open": OrderSide.BUY_TO_OPEN,
        "sell": OrderSide.SELL_TO_CLOSE,
        "close": OrderSide.SELL_TO_CLOSE,
        "sell_to_close": OrderSide.SELL_TO_CLOSE,
    }
    try:
        return aliases[normalized]
    except KeyError as exc:
        raise DataInvalidError(f"unknown broker order side: {value!r}") from exc
