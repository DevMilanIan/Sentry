from __future__ import annotations

import inspect
from abc import ABC, abstractmethod
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from dataclasses import field as dataclass_field
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.domain.enums import BrokerAction, ExecutionEnvironment, OrderSide
from app.domain.models import (
    AccountSnapshot,
    BrokerCommandIntent,
    BrokerOrder,
    BrokerReview,
    Fill,
    OptionContract,
    OptionQuote,
    Position,
    TradeProposal,
    sha256_json,
)
from app.exceptions import SafetyCriticalError


class BrokerValueModel(BaseModel):
    """Strict immutable values published by broker adapters."""

    model_config = ConfigDict(extra="forbid", frozen=True)


@dataclass(frozen=True, slots=True)
class CapabilityDescriptor:
    """A discovered broker capability and the exact accepted input schema."""

    capability: str
    tool_name: str
    schema_version: str
    schema_hash: str
    side_effect_free: bool
    input_schema: dict[str, Any] = dataclass_field(default_factory=dict)
    available: bool = True
    annotations: dict[str, Any] = dataclass_field(default_factory=dict)

    @classmethod
    def from_schema(
        cls,
        *,
        capability: str,
        tool_name: str,
        schema_version: str,
        input_schema: Mapping[str, Any],
        side_effect_free: bool,
        available: bool = True,
        annotations: Mapping[str, Any] | None = None,
    ) -> CapabilityDescriptor:
        schema = dict(input_schema)
        return cls(
            capability=capability,
            tool_name=tool_name,
            schema_version=schema_version,
            input_schema=schema,
            schema_hash=sha256_json(schema),
            available=available,
            side_effect_free=side_effect_free,
            annotations=dict(annotations or {}),
        )

    def validate(self, arguments: Mapping[str, Any]) -> dict[str, Any]:
        """Validate common JSON-Schema constraints without mutating arguments.

        MCP schemas are retained verbatim and the transport remains the final
        validator.  This intentionally small validator covers the object,
        required/property, primitive type, enum, and numeric constraints used
        by broker order tools, allowing the exact checked mapping to be stored
        in ``BrokerCommandIntent`` without adding another runtime dependency.
        """

        normalized = dict(arguments)
        _validate_schema_value(normalized, self.input_schema, path="$", strict_root=True)
        return normalized


class BrokerCapabilities(BrokerValueModel):
    """Capability-oriented view; callers never need raw MCP tool names."""

    adapter_name: str
    adapter_version: str
    discovered_at: datetime
    descriptors: tuple[CapabilityDescriptor, ...] = ()
    account_state: bool = False
    positions: bool = False
    orders: bool = False
    review_option_orders: bool = False
    place_option_orders: bool = False
    cancel_option_orders: bool = False
    reconcile: bool = True
    external_writes_enabled: bool = False
    execution_ready: bool = False
    issues: tuple[str, ...] = ()

    @field_validator("discovered_at")
    @classmethod
    def discovered_at_is_utc(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("discovered_at must be timezone-aware")
        return value.astimezone(UTC)

    def descriptor_for(self, capability: str) -> CapabilityDescriptor | None:
        return next(
            (
                descriptor
                for descriptor in self.descriptors
                if descriptor.capability == capability and descriptor.available
            ),
            None,
        )

    def descriptor_for_action(self, action: BrokerAction) -> CapabilityDescriptor | None:
        return self.descriptor_for(action.value)

    def supports(self, capability: str) -> bool:
        aliases = {
            "get_account_state": self.account_state,
            "get_positions": self.positions,
            "get_orders": self.orders,
            "review_option_order": self.review_option_orders,
            "place_option_order": self.place_option_orders,
            "cancel_option_order": self.cancel_option_orders,
            "reconcile": self.reconcile,
        }
        return aliases.get(capability, self.descriptor_for(capability) is not None)


class ReconciliationReport(BrokerValueModel):
    environment: ExecutionEnvironment
    reconciled_at: datetime
    successful: bool
    observed_account: AccountSnapshot
    effective_account: AccountSnapshot
    position_count: int = Field(ge=0)
    open_order_count: int = Field(ge=0)
    discrepancies: tuple[str, ...] = ()
    details: dict[str, Any] = Field(default_factory=dict)

    @field_validator("reconciled_at")
    @classmethod
    def reconciled_at_is_utc(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("reconciled_at must be timezone-aware")
        return value.astimezone(UTC)


CommandIntentRecorder = Callable[[BrokerCommandIntent], Awaitable[None] | None]


def validate_command_for_capability(
    command: BrokerCommandIntent,
    capabilities: BrokerCapabilities,
) -> dict[str, Any]:
    """Validate schema identity, arguments, and duplicated exact-order fields."""

    descriptor = capabilities.descriptor_for_action(command.action)
    if descriptor is None:
        raise SafetyCriticalError(f"required capability missing: {command.action.value}")
    if command.capability_name != descriptor.tool_name:
        raise SafetyCriticalError("command tool name does not match discovered capability")
    if command.capability_schema_version != descriptor.schema_version:
        raise SafetyCriticalError("command schema version does not match discovery")
    if command.capability_schema_hash != descriptor.schema_hash:
        raise SafetyCriticalError("command schema hash does not match discovery")
    validated = descriptor.validate(command.validated_arguments)

    instrument = _first_argument(
        validated,
        "instrument_id",
        "option_instrument_id",
        "option_id",
        "instrument",
    )
    if instrument is not None and str(instrument) != command.instrument_id:
        raise SafetyCriticalError("validated arguments contain a different instrument")
    quantity = _first_argument(validated, "quantity", "qty")
    if quantity is not None and int(quantity) != command.quantity:
        raise SafetyCriticalError("validated arguments contain a different quantity")
    price = _first_argument(validated, "limit_price", "price")
    if price is not None:
        from decimal import Decimal, InvalidOperation

        try:
            argument_price = Decimal(str(price))
        except (InvalidOperation, ValueError) as exc:
            raise SafetyCriticalError("validated arguments contain an invalid limit price") from exc
        if argument_price != command.limit_price:
            raise SafetyCriticalError("validated arguments contain a different limit price")
    side = _first_argument(validated, "side")
    if side is not None:
        allowed = {command.side.value}
        allowed.add("buy" if command.side is OrderSide.BUY_TO_OPEN else "sell")
        if str(side).lower() not in allowed:
            raise SafetyCriticalError("validated arguments contain a different order side")
    return validated


def _first_argument(arguments: Mapping[str, Any], *keys: str) -> Any:
    for key in keys:
        if key in arguments and arguments[key] is not None:
            return arguments[key]
    return None


def _validate_schema_value(
    value: Any,
    schema: Mapping[str, Any],
    *,
    path: str,
    strict_root: bool = False,
) -> None:
    """Validate the conservative JSON-Schema subset used by MCP order tools."""

    if not schema:
        return
    if "const" in schema and value != schema["const"]:
        raise ValueError(f"{path} must equal {schema['const']!r}")
    if "enum" in schema and value not in schema["enum"]:
        raise ValueError(f"{path} is not one of the allowed values")

    alternatives = schema.get("oneOf") or schema.get("anyOf")
    if isinstance(alternatives, list):
        failures = 0
        for alternative in alternatives:
            try:
                _validate_schema_value(value, alternative, path=path)
            except (TypeError, ValueError):
                failures += 1
        if failures == len(alternatives):
            raise ValueError(f"{path} did not match any allowed schema")
        return

    expected = schema.get("type")
    if isinstance(expected, list):
        for candidate in expected:
            try:
                _validate_schema_value(value, {**schema, "type": candidate}, path=path)
            except (TypeError, ValueError):
                continue
            return
        raise TypeError(f"{path} has an invalid type")

    type_checks: dict[str, tuple[type[Any], ...]] = {
        "object": (dict,),
        "array": (list, tuple),
        "string": (str,),
        "integer": (int,),
        "number": (int, float),
        "boolean": (bool,),
        "null": (type(None),),
    }
    if isinstance(expected, str) and expected in type_checks:
        if expected in {"integer", "number"} and isinstance(value, bool):
            raise TypeError(f"{path} must be {expected}")
        if not isinstance(value, type_checks[expected]):
            # Decimal is deliberately accepted as a JSON number; canonical
            # serialization converts it safely before transport.
            from decimal import Decimal

            if expected != "number" or not isinstance(value, Decimal):
                raise TypeError(f"{path} must be {expected}")

    if isinstance(value, dict):
        required = schema.get("required", ())
        for key in required:
            if key not in value:
                raise ValueError(f"{path}.{key} is required")
        properties = schema.get("properties", {})
        if schema.get("additionalProperties") is False or strict_root:
            # At the root, reject extras only when the schema explicitly does.
            if schema.get("additionalProperties") is False:
                extras = set(value).difference(properties)
                if extras:
                    raise ValueError(f"{path} contains unexpected keys: {sorted(extras)!r}")
        for key, item in value.items():
            child_schema = properties.get(key)
            if isinstance(child_schema, Mapping):
                _validate_schema_value(item, child_schema, path=f"{path}.{key}")
    elif isinstance(value, (list, tuple)) and isinstance(schema.get("items"), Mapping):
        for index, item in enumerate(value):
            _validate_schema_value(item, schema["items"], path=f"{path}[{index}]")

    if isinstance(value, (int, float)) and not isinstance(value, bool):
        if "minimum" in schema and value < schema["minimum"]:
            raise ValueError(f"{path} is less than minimum")
        if "maximum" in schema and value > schema["maximum"]:
            raise ValueError(f"{path} exceeds maximum")
    if isinstance(value, str):
        if "minLength" in schema and len(value) < schema["minLength"]:
            raise ValueError(f"{path} is shorter than minLength")
        if "maxLength" in schema and len(value) > schema["maxLength"]:
            raise ValueError(f"{path} is longer than maxLength")


class Broker(ABC):
    """Account/execution boundary shared by simulated, shadow, and live brokers.

    Broad market-data discovery intentionally does not belong here.  A broker
    that simulates fills receives already-normalized :class:`OptionQuote`
    events through ``consume_quote`` rather than owning a market-data feed.
    """

    @abstractmethod
    async def get_capabilities(self) -> BrokerCapabilities:
        raise NotImplementedError

    @abstractmethod
    async def get_observed_broker_account_state(self) -> AccountSnapshot:
        raise NotImplementedError

    @abstractmethod
    async def get_effective_execution_account_state(self) -> AccountSnapshot:
        raise NotImplementedError

    async def get_account_state(self) -> AccountSnapshot:
        """Compatibility alias for the account used by risk/execution."""

        return await self.get_effective_execution_account_state()

    @abstractmethod
    async def get_positions(self) -> tuple[Position, ...]:
        raise NotImplementedError

    @abstractmethod
    async def get_orders(self) -> tuple[BrokerOrder, ...]:
        raise NotImplementedError

    @abstractmethod
    async def review_option_order(self, proposal: TradeProposal) -> BrokerReview:
        raise NotImplementedError

    @abstractmethod
    async def record_broker_command_intent(self, command: BrokerCommandIntent) -> None:
        """Durably hand a typed command to the configured recorder, idempotently."""

        raise NotImplementedError

    @abstractmethod
    async def place_option_order(
        self,
        command: BrokerCommandIntent,
        contract: OptionContract,
    ) -> BrokerOrder:
        raise NotImplementedError

    @abstractmethod
    async def cancel_option_order(
        self,
        command: BrokerCommandIntent,
        order_id: UUID | str,
    ) -> BrokerOrder:
        raise NotImplementedError

    @abstractmethod
    async def reconcile(self) -> ReconciliationReport:
        raise NotImplementedError

    async def consume_quote(self, quote: OptionQuote) -> tuple[Fill, ...]:
        """Consume an external quote event if this adapter models local fills."""

        del quote
        return ()


# Longer, explicit name retained for application wiring and documentation.
BrokerAccountExecution = Broker


class IntentRecordingBroker(Broker):
    """Shared exact-intent recording and idempotency checks for adapters."""

    def __init__(self, *, command_recorder: CommandIntentRecorder | None = None) -> None:
        self._command_recorder = command_recorder
        self._recorded_commands: dict[UUID, BrokerCommandIntent] = {}
        self._command_idempotency: dict[str, UUID] = {}

    @property
    def recorded_command_intents(self) -> tuple[BrokerCommandIntent, ...]:
        return tuple(self._recorded_commands.values())

    async def record_broker_command_intent(self, command: BrokerCommandIntent) -> None:
        existing = self._recorded_commands.get(command.command_intent_id)
        if existing is not None:
            if existing.command_hash != command.command_hash:
                raise SafetyCriticalError("command intent ID was reused with different content")
            return

        existing_id = self._command_idempotency.get(command.idempotency_key)
        if existing_id is not None:
            other = self._recorded_commands[existing_id]
            if other.command_hash != command.command_hash:
                raise SafetyCriticalError(
                    "broker idempotency key was reused for a different command"
                )
            return

        # Persistence must succeed before the in-process adapter considers the
        # intent recorded; otherwise a callback failure followed by a retry
        # could reach a write without a durable intent.
        if self._command_recorder is not None:
            result = self._command_recorder(command)
            if inspect.isawaitable(result):
                await result
        self._recorded_commands[command.command_intent_id] = command
        self._command_idempotency[command.idempotency_key] = command.command_intent_id
