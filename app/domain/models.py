from __future__ import annotations

import hashlib
import json
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.domain.enums import (
    AccountKind,
    AttentionLevel,
    BrokerAction,
    Direction,
    ExecutionEnvironment,
    FirewallDisposition,
    JudgeDecision,
    OptionType,
    OrderSide,
    OrderState,
    RuntimeSafetyState,
    SelectorStatus,
    TradingMode,
)

Money = Decimal


def canonical_json(value: Any) -> str:
    """Return stable JSON suitable for hashes and audit comparisons."""

    def default(item: Any) -> str:
        if isinstance(item, (datetime, date, UUID, Decimal)):
            return str(item)
        raise TypeError(f"Unsupported canonical JSON type: {type(item)!r}")

    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=default)


def sha256_json(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def utc_now() -> datetime:
    """Infrastructure-only ID timestamp helper; trading logic receives an injected Clock."""

    return datetime.now(UTC)


class DomainModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, use_enum_values=False)


class TimestampedModel(DomainModel):
    created_at: datetime

    @field_validator("created_at")
    @classmethod
    def created_at_is_utc(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("timestamp must be timezone-aware")
        return value.astimezone(UTC)


class ProviderMetadata(DomainModel):
    provider: str
    capability_version: str
    observed_at: datetime
    effective_at: datetime
    source_id: str | None = None

    @field_validator("observed_at", "effective_at")
    @classmethod
    def timestamp_is_aware(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("provider timestamps must be timezone-aware")
        return value.astimezone(UTC)


class EquityQuote(DomainModel):
    snapshot_id: UUID = Field(default_factory=uuid4)
    symbol: str = Field(min_length=1, max_length=12)
    bid: Money = Field(ge=0)
    ask: Money = Field(ge=0)
    last: Money = Field(ge=0)
    volume: int = Field(ge=0)
    metadata: ProviderMetadata

    @model_validator(mode="after")
    def valid_market(self) -> EquityQuote:
        if self.ask and self.bid > self.ask:
            raise ValueError("bid cannot exceed ask")
        return self


class OptionContract(DomainModel):
    instrument_id: str = Field(min_length=1)
    symbol: str = Field(min_length=1, max_length=12)
    option_type: OptionType
    strike: Money = Field(gt=0)
    expiration: date
    multiplier: int = Field(default=100, gt=0)


class OptionQuote(DomainModel):
    snapshot_id: UUID = Field(default_factory=uuid4)
    contract: OptionContract
    bid: Money = Field(ge=0)
    ask: Money = Field(ge=0)
    last: Money | None = Field(default=None, ge=0)
    mark: Money | None = Field(default=None, ge=0)
    volume: int | None = Field(default=None, ge=0)
    open_interest: int | None = Field(default=None, ge=0)
    implied_volatility: Decimal | None = Field(default=None, ge=0)
    delta: Decimal | None = None
    bid_size: int | None = Field(default=None, ge=0)
    ask_size: int | None = Field(default=None, ge=0)
    metadata: ProviderMetadata

    @model_validator(mode="after")
    def valid_market(self) -> OptionQuote:
        if self.ask and self.bid > self.ask:
            raise ValueError("bid cannot exceed ask")
        return self


class AccountSnapshot(TimestampedModel):
    snapshot_id: UUID = Field(default_factory=uuid4)
    environment: ExecutionEnvironment
    account_kind: AccountKind
    account_fingerprint: str | None = None
    cash: Money = Field(ge=0)
    buying_power: Money = Field(ge=0)
    open_option_risk: Money = Field(default=Decimal("0"), ge=0)
    open_positions: int = Field(default=0, ge=0)
    new_entries_today: int = Field(default=0, ge=0)
    as_of: datetime
    is_authenticated: bool
    state_known: bool

    @field_validator("as_of")
    @classmethod
    def as_of_is_utc(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("as_of must be timezone-aware")
        return value.astimezone(UTC)

    @model_validator(mode="after")
    def kind_matches_environment(self) -> AccountSnapshot:
        if (
            self.environment is ExecutionEnvironment.LIVE
            and self.account_kind is not AccountKind.BROKER_OBSERVED
        ):
            raise ValueError("LIVE effective account state must be broker observed")
        return self


class SentinelEvent(TimestampedModel):
    event_id: UUID = Field(default_factory=uuid4)
    event_type: str
    source: str
    effective_at: datetime
    tickers: tuple[str, ...] = ()
    severity: int = Field(ge=0, le=5)
    deduplication_key: str
    raw_reference_ids: tuple[str, ...] = ()
    payload: dict[str, Any] = Field(default_factory=dict)


class CandidatePacket(TimestampedModel):
    packet_id: UUID = Field(default_factory=uuid4)
    run_id: UUID
    symbol: str
    attention: AttentionLevel
    surveillance_score: Decimal = Field(ge=0, le=100)
    facts: dict[str, Any]
    source_ids: tuple[str, ...]
    market_snapshot_ids: tuple[UUID, ...]
    available_at: datetime

    @property
    def content_hash(self) -> str:
        return sha256_json(self.model_dump(mode="json", exclude={"created_at"}))


class AgentAnalysis(TimestampedModel):
    analysis_id: UUID = Field(default_factory=uuid4)
    packet_id: UUID
    role: str
    model_name: str
    model_digest: str | None = None
    prompt_version: str
    output: dict[str, Any]
    referenced_fact_ids: tuple[str, ...] = ()
    output_hash: str
    latency_ms: int = Field(ge=0)


class JudgeOutput(DomainModel):
    decision: JudgeDecision
    directional_thesis: Direction
    selected_candidate_rank: int | None = Field(ge=1)
    confidence: Decimal = Field(ge=0, le=1)
    expected_time_window: str
    catalyst_strength: Decimal = Field(ge=0, le=1)
    contract_quality_critique: str
    thesis: str
    invalidation_conditions: tuple[str, ...]
    recheck_conditions: tuple[str, ...]
    reasons_to_abstain: tuple[str, ...]
    rationale: str

    @model_validator(mode="after")
    def selected_rank_only_on_pass(self) -> JudgeOutput:
        if self.decision is JudgeDecision.PASS and self.selected_candidate_rank is None:
            raise ValueError("PASS requires a selected contract rank")
        if self.decision is not JudgeDecision.PASS and self.selected_candidate_rank is not None:
            raise ValueError("non-PASS output cannot select a contract")
        return self


class ContractSelection(DomainModel):
    status: SelectorStatus
    ranked_quotes: tuple[OptionQuote, ...] = ()
    rejected_reasons: dict[str, tuple[str, ...]] = Field(default_factory=dict)

    @model_validator(mode="after")
    def status_matches_results(self) -> ContractSelection:
        if self.status is SelectorStatus.CONTRACT_FOUND and not self.ranked_quotes:
            raise ValueError("CONTRACT_FOUND requires at least one quote")
        if self.status is SelectorStatus.NO_CONTRACT and self.ranked_quotes:
            raise ValueError("NO_CONTRACT cannot contain ranked quotes")
        return self


class TradeProposal(TimestampedModel):
    proposal_id: UUID = Field(default_factory=uuid4)
    environment: ExecutionEnvironment
    namespace: str
    packet_id: UUID
    symbol: str
    contract: OptionContract
    side: OrderSide
    quantity: int = Field(gt=0)
    limit_price: Money = Field(gt=0)
    quote_snapshot_id: UUID
    quote_as_of: datetime
    policy_version: str
    risk_config_version: str
    thesis: str
    invalidation_conditions: tuple[str, ...]

    @property
    def max_loss(self) -> Money:
        return self.limit_price * self.quantity * self.contract.multiplier

    @property
    def order_fingerprint(self) -> str:
        fields = {
            "environment": self.environment.value,
            "namespace": self.namespace,
            "contract": self.contract.instrument_id,
            "side": self.side.value,
            "quantity": self.quantity,
            "limit_price": str(self.limit_price),
            "proposal_id": str(self.proposal_id),
        }
        return sha256_json(fields)


class RiskDecision(TimestampedModel):
    decision_id: UUID = Field(default_factory=uuid4)
    environment: ExecutionEnvironment
    proposal_id: UUID
    allowed: bool
    failed_rules: tuple[str, ...]
    passed_rules: tuple[str, ...]
    account_snapshot_id: UUID
    proposed_max_loss: Money = Field(ge=0)
    resulting_aggregate_risk: Money = Field(ge=0)
    data_fresh: bool
    risk_config_version: str

    @model_validator(mode="after")
    def consistent_result(self) -> RiskDecision:
        if self.allowed and self.failed_rules:
            raise ValueError("allowed decision cannot contain failed rules")
        if not self.allowed and not self.failed_rules:
            raise ValueError("denied decision requires a failed rule")
        return self


class ExactApproval(TimestampedModel):
    approval_id: UUID = Field(default_factory=uuid4)
    environment: ExecutionEnvironment
    namespace: str
    proposal_id: UUID
    order_fingerprint: str
    maximum_limit_price: Money = Field(gt=0)
    expires_at: datetime
    approved_by: str
    rejected: bool = False

    def is_valid_for(self, proposal: TradeProposal, at: datetime) -> bool:
        return (
            not self.rejected
            and self.environment is proposal.environment
            and self.namespace == proposal.namespace
            and self.proposal_id == proposal.proposal_id
            and self.order_fingerprint == proposal.order_fingerprint
            and proposal.limit_price <= self.maximum_limit_price
            and at <= self.expires_at
        )


class BrokerReview(TimestampedModel):
    review_id: UUID = Field(default_factory=uuid4)
    environment: ExecutionEnvironment
    proposal_id: UUID
    accepted: bool
    warnings: tuple[str, ...] = ()
    raw_reference: str | None = None
    side_effect_free: bool = True


class OrderIntent(TimestampedModel):
    intent_id: UUID = Field(default_factory=uuid4)
    environment: ExecutionEnvironment
    namespace: str
    proposal_id: UUID
    risk_decision_id: UUID
    approval_id: UUID | None
    review_id: UUID
    order_fingerprint: str
    idempotency_key: str
    action: BrokerAction
    state: OrderState = OrderState.INTENT_PERSISTED


class BrokerCommandIntent(TimestampedModel):
    command_intent_id: UUID = Field(default_factory=uuid4)
    order_intent_id: UUID
    environment: ExecutionEnvironment
    namespace: str
    action: BrokerAction
    capability_name: str
    capability_schema_version: str
    capability_schema_hash: str
    instrument_id: str
    side: OrderSide
    quantity: int = Field(gt=0)
    limit_price: Money = Field(gt=0)
    time_in_force: str = "day"
    validated_arguments: dict[str, Any]
    proposal_id: UUID
    risk_decision_id: UUID
    approval_id: UUID | None
    quote_snapshot_id: UUID
    broker_observed_account_snapshot_id: UUID | None
    effective_account_snapshot_id: UUID
    policy_version: str
    order_fingerprint: str
    idempotency_key: str

    @property
    def command_hash(self) -> str:
        return sha256_json(self.model_dump(mode="json", exclude={"created_at"}))


class FirewallDecision(TimestampedModel):
    firewall_event_id: UUID = Field(default_factory=uuid4)
    command_intent_id: UUID
    environment: ExecutionEnvironment
    disposition: FirewallDisposition
    reason: str
    transmitted: bool

    @model_validator(mode="after")
    def blocked_is_never_transmitted(self) -> FirewallDecision:
        if self.disposition is not FirewallDisposition.AUTHORIZED_LIVE and self.transmitted:
            raise ValueError("blocked firewall decision cannot be transmitted")
        return self


class BrokerOrder(TimestampedModel):
    order_id: UUID = Field(default_factory=uuid4)
    broker_order_id: str | None = None
    intent_id: UUID
    environment: ExecutionEnvironment
    state: OrderState
    contract: OptionContract
    side: OrderSide
    quantity: int = Field(gt=0)
    filled_quantity: int = Field(default=0, ge=0)
    limit_price: Money = Field(gt=0)
    average_fill_price: Money | None = Field(default=None, ge=0)
    submitted_at: datetime | None = None


class Fill(TimestampedModel):
    fill_id: UUID = Field(default_factory=uuid4)
    order_id: UUID
    quantity: int = Field(gt=0)
    price: Money = Field(gt=0)
    market_event_ids: tuple[str, ...]
    fill_model_version: str
    deterministic_seed: int
    reason: str


class Position(TimestampedModel):
    position_id: UUID = Field(default_factory=uuid4)
    environment: ExecutionEnvironment
    contract: OptionContract
    quantity: int = Field(gt=0)
    average_entry_price: Money = Field(gt=0)
    current_bid: Money = Field(ge=0)
    current_ask: Money = Field(ge=0)
    realized_pnl: Money = Decimal("0")
    best_unrealized_pnl: Money = Decimal("0")
    worst_unrealized_pnl: Money = Decimal("0")
    thesis_id: UUID
    invalidation_conditions: tuple[str, ...]
    exit_policy_version: str


class HealthSnapshot(TimestampedModel):
    run_id: UUID
    environment: ExecutionEnvironment
    safety_state: RuntimeSafetyState
    trading_mode: TradingMode
    database_healthy: bool
    market_data_fresh: bool
    broker_state_known: bool
    model_healthy: bool
    reconciled: bool
    details: dict[str, Any] = Field(default_factory=dict)
