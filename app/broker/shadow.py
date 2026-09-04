from __future__ import annotations

import asyncio
import inspect
from collections.abc import Awaitable, Callable
from datetime import date, timedelta
from decimal import Decimal
from typing import Literal
from uuid import UUID

from pydantic import Field

from app.broker.base import (
    BrokerCapabilities,
    CommandIntentRecorder,
    IntentRecordingBroker,
    ReconciliationReport,
)
from app.broker.fill_models import ConservativeFillModel, FillModel
from app.broker.robinhood_mcp import RobinhoodReadReviewClient
from app.broker.shadow_ledger import (
    DepositRecord,
    ExpirationPolicy,
    ExpirationResult,
    LedgerSnapshot,
    ShadowLedger,
)
from app.clock.base import Clock
from app.domain.enums import (
    AccountKind,
    BrokerAction,
    ExecutionEnvironment,
    FirewallDisposition,
    OrderState,
)
from app.domain.models import (
    AccountSnapshot,
    BrokerCommandIntent,
    BrokerOrder,
    BrokerReview,
    Fill,
    OptionContract,
    OptionQuote,
    Position,
    TimestampedModel,
    TradeProposal,
    sha256_json,
)
from app.exceptions import SafetyCriticalError, SentinelError
from app.safety.write_firewall import DenyAllWriteFirewall

ShadowLedgerStateRecorder = Callable[[LedgerSnapshot], Awaitable[None] | None]
ShadowReviewRecorder = Callable[["BrokerShadowReviewEvidence"], Awaitable[None] | None]
_TERMINAL_ORDER_STATES = frozenset(
    {OrderState.FILLED, OrderState.CANCELED, OrderState.REJECTED, OrderState.EXPIRED}
)


class BrokerShadowReviewEvidence(TimestampedModel):
    """Distinct, durable evidence for one local and broker-observed review pair."""

    record_kind: Literal["broker_shadow_review_evidence_v1"] = (
        "broker_shadow_review_evidence_v1"
    )
    environment: Literal["DEMO"] = "DEMO"
    namespace: str = Field(min_length=1)
    proposal_id: UUID
    combined_review_id: UUID
    shadow_review: BrokerReview
    broker_observed_review: BrokerReview | None
    broker_review_status: Literal["AVAILABLE", "UNAVAILABLE"]
    broker_error_type: str | None = None
    observed_state_before_hash: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    observed_state_after_hash: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    observed_state_unchanged: bool = False


def _observed_state_hash(
    account: AccountSnapshot,
    positions: tuple[Position, ...],
    orders: tuple[BrokerOrder, ...],
) -> str:
    """Hash stable economic/order state while ignoring read timestamps and local parse IDs."""

    position_state = sorted(
        (
            {
                "instrument_id": item.contract.instrument_id,
                "contract": item.contract.model_dump(mode="json"),
                "quantity": item.quantity,
                "average_entry_price": str(item.average_entry_price),
                "realized_pnl": str(item.realized_pnl),
            }
            for item in positions
        ),
        key=lambda item: (item["instrument_id"], sha256_json(item)),
    )
    order_state = sorted(
        (
            {
                "broker_order_id": item.broker_order_id,
                "contract": item.contract.model_dump(mode="json"),
                "side": item.side.value,
                "quantity": item.quantity,
                "filled_quantity": item.filled_quantity,
                "limit_price": str(item.limit_price),
                "average_fill_price": (
                    str(item.average_fill_price) if item.average_fill_price is not None else None
                ),
                "state": item.state.value,
                "submitted_at": item.submitted_at,
            }
            for item in orders
        ),
        key=lambda item: (str(item["broker_order_id"]), sha256_json(item)),
    )
    return sha256_json(
        {
            "account": account.model_dump(
                mode="json", exclude={"snapshot_id", "created_at", "as_of"}
            ),
            "positions": position_state,
            "orders": order_state,
        }
    )


class RobinhoodShadowBroker(IntentRecordingBroker):
    """Real Robinhood reads/reviews plus a strictly isolated local ledger.

    Its constructor accepts only the capability-specific read/review facade.
    Unlike the Live adapter, this object has no generic or write transport
    member.  Place/cancel intents terminate at ``DenyAllWriteFirewall`` before
    being applied to the local ledger.
    """

    adapter_version = "robinhood-shadow-broker-v1"

    def __init__(
        self,
        *,
        read_client: RobinhoodReadReviewClient,
        clock: Clock,
        initial_cash: Decimal = Decimal("25"),
        fill_model: FillModel | None = None,
        fill_seed: int = 0,
        max_quote_age: timedelta = timedelta(seconds=30),
        namespace: str = "demo",
        firewall: DenyAllWriteFirewall | None = None,
        command_recorder: CommandIntentRecorder | None = None,
        meaningful_external_balance: Decimal = Decimal("0"),
        state_recorder: ShadowLedgerStateRecorder | None = None,
        review_recorder: ShadowReviewRecorder | None = None,
        initial_state: LedgerSnapshot | None = None,
        expected_account_fingerprint: str | None = None,
        acknowledged_historical_order_ids: frozenset[str] = frozenset(),
    ) -> None:
        super().__init__(command_recorder=command_recorder)
        selected_firewall = firewall or DenyAllWriteFirewall(clock)
        if not isinstance(selected_firewall, DenyAllWriteFirewall):
            raise SafetyCriticalError("broker-shadow requires the deny-all write firewall")
        if not meaningful_external_balance.is_finite() or meaningful_external_balance < 0:
            raise ValueError("meaningful external balance threshold cannot be negative")
        if expected_account_fingerprint is not None and not expected_account_fingerprint.strip():
            raise ValueError("expected account fingerprint cannot be empty")
        if any(not value.strip() for value in acknowledged_historical_order_ids):
            raise ValueError("historical order IDs cannot be empty")
        self._clock = clock
        self._namespace = namespace
        self._read_client = read_client
        self._firewall = selected_firewall
        self._meaningful_external_balance = meaningful_external_balance
        self._state_recorder = state_recorder
        self._review_recorder = review_recorder
        self._state_dirty = False
        self._expected_account_fingerprint = expected_account_fingerprint
        self._historical_order_ids = frozenset(acknowledged_historical_order_ids)
        self._ledger = ShadowLedger(
            clock=clock,
            initial_cash=initial_cash,
            account_kind=AccountKind.SHADOW,
            fill_model=fill_model or ConservativeFillModel(seed_salt=fill_seed),
            max_quote_age=max_quote_age,
            namespace=namespace,
        )
        if initial_state is not None:
            self._ledger.restore_state(initial_state)
            self._restore_commands(initial_state)
        self._lock = asyncio.Lock()
        self._capabilities: BrokerCapabilities | None = None
        self._last_broker_review: BrokerReview | None = None
        self._last_shadow_review: BrokerReview | None = None
        self._last_review_evidence: BrokerShadowReviewEvidence | None = None

    @property
    def ledger(self) -> ShadowLedger:
        return self._ledger

    @property
    def state_persisted(self) -> bool:
        return not self._state_dirty

    def export_state(self) -> LedgerSnapshot:
        return self._ledger.export_state(recorded_commands=self.recorded_command_intents)

    async def flush_state(self) -> None:
        async with self._lock:
            await self._persist_state()

    def _restore_commands(self, snapshot: LedgerSnapshot) -> None:
        commands = {item.command_intent_id: item for item in snapshot.recorded_commands}
        keys = {item.idempotency_key: item.command_intent_id for item in snapshot.recorded_commands}
        if len(commands) != len(snapshot.recorded_commands) or len(keys) != len(commands):
            raise SafetyCriticalError(
                "restored shadow broker contains duplicate command identities"
            )
        for item in snapshot.orders:
            recorded = commands.get(item.command.command_intent_id)
            if recorded is None or recorded.command_hash != item.command.command_hash:
                raise SafetyCriticalError("restored shadow order lacks its exact recorded command")
            if (
                snapshot.idempotency_order_ids.get(item.command.idempotency_key)
                != item.published.order_id
            ):
                raise SafetyCriticalError("restored shadow placement idempotency target differs")
        orders = {item.published.order_id: item for item in snapshot.orders}
        for key, order_id in snapshot.idempotency_order_ids.items():
            command_id = keys.get(key)
            if command_id is None:
                raise SafetyCriticalError(
                    "restored shadow idempotency key lacks its recorded command"
                )
            command = commands[command_id]
            target = orders[order_id]
            if command.instrument_id != target.published.contract.instrument_id:
                raise SafetyCriticalError(
                    "restored shadow command references a different instrument"
                )
            if (
                command.action is BrokerAction.PLACE_OPTION_ORDER
                and command.command_hash != target.command.command_hash
            ):
                raise SafetyCriticalError(
                    "restored shadow placement command differs from its order"
                )
        self._recorded_commands = commands
        self._command_idempotency = keys

    def _ensure_clean(self) -> None:
        if self._state_dirty:
            raise SafetyCriticalError("shadow ledger has an unpersisted mutation")

    def _begin_mutation(self) -> None:
        if self._state_recorder is not None:
            self._state_dirty = True

    async def _persist_state(self) -> None:
        if self._state_recorder is None:
            return
        self._state_dirty = True
        result = self._state_recorder(self.export_state())
        if inspect.isawaitable(result):
            await result
        self._state_dirty = False

    async def _deny_write(self, command: BrokerCommandIntent) -> None:
        if (
            command.environment is not ExecutionEnvironment.DEMO
            or command.namespace != self._namespace
        ):
            raise SafetyCriticalError("shadow command does not match the immutable runtime binding")
        if not self._firewall.healthcheck():
            raise SafetyCriticalError("deny-all external write firewall is unhealthy")
        decision = await self._firewall.evaluate(command)
        if (
            decision.transmitted
            or decision.disposition is not FirewallDisposition.BLOCKED_SHADOW
            or decision.command_intent_id != command.command_intent_id
            or decision.environment is not ExecutionEnvironment.DEMO
        ):
            raise SafetyCriticalError("broker-shadow write firewall did not deny the exact command")

    @property
    def last_broker_observed_review(self) -> BrokerReview | None:
        return self._last_broker_review

    @property
    def last_shadow_execution_review(self) -> BrokerReview | None:
        return self._last_shadow_review

    @property
    def last_review_evidence(self) -> BrokerShadowReviewEvidence | None:
        return self._last_review_evidence

    async def get_capabilities(self) -> BrokerCapabilities:
        discovered = await self._read_client.get_capabilities()
        issues = list(discovered.issues)
        if not self._firewall.healthcheck():
            issues.append("deny-all external write firewall is unhealthy")
        if self._state_dirty:
            issues.append("shadow ledger has an unpersisted mutation")
        self._capabilities = discovered.model_copy(
            update={
                "adapter_name": "RobinhoodShadowBroker",
                "adapter_version": self.adapter_version,
                "external_writes_enabled": False,
                "execution_ready": discovered.execution_ready and not issues,
                "issues": tuple(issues),
            }
        )
        return self._capabilities

    async def get_observed_broker_account_state(self) -> AccountSnapshot:
        account = await self._read_client.get_account_state()
        if account.account_kind is not AccountKind.BROKER_OBSERVED:
            raise SafetyCriticalError("shadow read client returned a non-broker account")
        if account.environment is not ExecutionEnvironment.DEMO:
            raise SafetyCriticalError("shadow read client returned a non-DEMO account view")
        if not account.account_fingerprint:
            raise SafetyCriticalError("shadow read client returned an account without identity")
        if (
            self._expected_account_fingerprint is not None
            and account.account_fingerprint != self._expected_account_fingerprint
        ):
            raise SafetyCriticalError("shadow read client returned a different selected account")
        return account

    async def get_effective_execution_account_state(self) -> AccountSnapshot:
        return self._ledger.account_snapshot()

    async def get_positions(self) -> tuple[Position, ...]:
        """Return effective hypothetical positions, never real broker positions."""

        return self._ledger.get_positions()

    async def get_orders(self) -> tuple[BrokerOrder, ...]:
        """Return effective hypothetical orders, never real broker orders."""

        return self._ledger.get_orders()

    async def get_observed_broker_positions(self) -> tuple[Position, ...]:
        return await self._read_client.get_positions()

    async def get_observed_broker_orders(self) -> tuple[BrokerOrder, ...]:
        return await self._read_client.get_orders()

    async def review_option_order(self, proposal: TradeProposal) -> BrokerReview:
        if proposal.environment is not ExecutionEnvironment.DEMO:
            raise SafetyCriticalError("broker-shadow received non-DEMO proposal")
        async with self._lock:
            self._ensure_clean()
            self._begin_mutation()
            shadow_review = self._ledger.review(proposal)
            await self._persist_state()
            self._last_shadow_review = shadow_review

        broker_review: BrokerReview | None = None
        broker_error_type: str | None = None
        before_hash: str | None = None
        after_hash: str | None = None
        try:
            before = await asyncio.gather(
                self.get_observed_broker_account_state(),
                self.get_observed_broker_positions(),
                self.get_observed_broker_orders(),
            )
            before_hash = _observed_state_hash(before[0], before[1], before[2])
            broker_review = await self._read_client.review_option_order(proposal)
            after = await asyncio.gather(
                self.get_observed_broker_account_state(),
                self.get_observed_broker_positions(),
                self.get_observed_broker_orders(),
            )
            after_hash = _observed_state_hash(after[0], after[1], after[2])
        except SentinelError as exc:
            # Keep error detail out of qualification rows; the exception class is
            # sufficient to distinguish connectivity/schema/auth failures.
            broker_error_type = type(exc).__name__
        self._last_broker_review = broker_review

        observed_state_unchanged = (
            before_hash is not None and after_hash is not None and before_hash == after_hash
        )

        warnings = [f"shadow: {item}" for item in shadow_review.warnings]
        if broker_review is not None:
            warnings.extend(f"broker-observed: {item}" for item in broker_review.warnings)
            if not broker_review.accepted:
                warnings.append(
                    "broker-observed review rejected; shadow acceptance remains separate"
                )
            if not broker_review.side_effect_free:
                warnings.append("broker-observed review did not assert a side-effect-free result")
        elif broker_error_type is not None:
            warnings.append(f"broker-observed review unavailable: {broker_error_type}")
        if not observed_state_unchanged:
            warnings.append("broker-observed state was not proven unchanged across review")
        reference = (
            f"shadow={shadow_review.review_id};broker={broker_review.review_id}"
            if broker_review is not None
            else f"shadow={shadow_review.review_id};broker=unavailable"
        )
        combined = BrokerReview(
            created_at=self._clock.now(),
            environment=ExecutionEnvironment.DEMO,
            proposal_id=proposal.proposal_id,
            accepted=(
                shadow_review.accepted
                and broker_review is not None
                and broker_review.side_effect_free
                and observed_state_unchanged
            ),
            warnings=tuple(warnings),
            raw_reference=reference,
            side_effect_free=(
                broker_review is not None
                and broker_review.side_effect_free
                and observed_state_unchanged
            ),
        )
        if self._review_recorder is not None:
            evidence = BrokerShadowReviewEvidence(
                created_at=self._clock.now(),
                namespace=self._namespace,
                proposal_id=proposal.proposal_id,
                combined_review_id=combined.review_id,
                shadow_review=shadow_review,
                broker_observed_review=broker_review,
                broker_review_status="AVAILABLE" if broker_review is not None else "UNAVAILABLE",
                broker_error_type=broker_error_type,
                observed_state_before_hash=before_hash,
                observed_state_after_hash=after_hash,
                observed_state_unchanged=observed_state_unchanged,
            )
            self._last_review_evidence = evidence
            recorded = self._review_recorder(evidence)
            if inspect.isawaitable(recorded):
                await recorded
        else:
            self._last_review_evidence = BrokerShadowReviewEvidence(
                created_at=self._clock.now(),
                namespace=self._namespace,
                proposal_id=proposal.proposal_id,
                combined_review_id=combined.review_id,
                shadow_review=shadow_review,
                broker_observed_review=broker_review,
                broker_review_status="AVAILABLE" if broker_review is not None else "UNAVAILABLE",
                broker_error_type=broker_error_type,
                observed_state_before_hash=before_hash,
                observed_state_after_hash=after_hash,
                observed_state_unchanged=observed_state_unchanged,
            )
        return combined

    async def place_option_order(
        self,
        command: BrokerCommandIntent,
        contract: OptionContract,
    ) -> BrokerOrder:
        if command.action is not BrokerAction.PLACE_OPTION_ORDER:
            raise SafetyCriticalError("shadow placement received a non-placement command")
        await self._read_client.validate_command(command)
        async with self._lock:
            self._ensure_clean()
            await self._deny_write(command)
            self._begin_mutation()
            await self.record_broker_command_intent(command)
            order = self._ledger.submit(command, contract)
            await self._persist_state()
            return order

    async def cancel_option_order(
        self,
        command: BrokerCommandIntent,
        order_id: UUID | str,
    ) -> BrokerOrder:
        if command.action is not BrokerAction.CANCEL_OPTION_ORDER:
            raise SafetyCriticalError("shadow cancellation received a non-cancel command")
        await self._read_client.validate_command(command)
        async with self._lock:
            self._ensure_clean()
            await self._deny_write(command)
            self._begin_mutation()
            await self.record_broker_command_intent(command)
            order = self._ledger.cancel(command, order_id)
            await self._persist_state()
            return order

    async def consume_quote(self, quote: OptionQuote) -> tuple[Fill, ...]:
        async with self._lock:
            self._ensure_clean()
            self._begin_mutation()
            fills = self._ledger.observe_quote(quote)
            await self._persist_state()
            return fills

    async def deposit(
        self,
        amount: Decimal,
        *,
        reference: str = "configured-shadow-scenario",
    ) -> DepositRecord:
        async with self._lock:
            self._ensure_clean()
            self._begin_mutation()
            deposit = self._ledger.deposit(amount, reference=reference)
            await self._persist_state()
            return deposit

    async def process_expirations(
        self,
        *,
        on_date: date | None = None,
        policy: ExpirationPolicy = ExpirationPolicy.DO_NOT_EXERCISE,
        cash_settlement_per_share: dict[str, Decimal] | None = None,
    ) -> ExpirationResult:
        async with self._lock:
            self._ensure_clean()
            self._begin_mutation()
            result = self._ledger.expire(
                on_date=on_date,
                policy=policy,
                cash_settlement_per_share=cash_settlement_per_share,
            )
            await self._persist_state()
            return result

    async def reconcile(self) -> ReconciliationReport:
        observed, observed_positions, observed_orders = await asyncio.gather(
            self.get_observed_broker_account_state(),
            self.get_observed_broker_positions(),
            self.get_observed_broker_orders(),
        )
        async with self._lock:
            local = self._ledger.reconciliation_report()
            effective = local.effective_account
            state_dirty = self._state_dirty
        observed_active_orders = tuple(
            order for order in observed_orders if order.state not in _TERMINAL_ORDER_STATES
        )
        historical_orders = tuple(
            order for order in observed_orders if order.state in _TERMINAL_ORDER_STATES
        )
        unacknowledged_history = tuple(
            order
            for order in historical_orders
            if order.broker_order_id not in self._historical_order_ids
        )
        discrepancies = list(local.discrepancies)
        threshold = self._meaningful_external_balance
        if observed.cash > threshold:
            discrepancies.append("unexpected real broker cash during BROKER_SHADOW")
        if observed.buying_power > threshold:
            discrepancies.append("unexpected real broker buying power during BROKER_SHADOW")
        if observed_positions:
            discrepancies.append("unexpected real broker option position during BROKER_SHADOW")
        if observed_active_orders:
            discrepancies.append("unexpected active real broker option order during BROKER_SHADOW")
        if unacknowledged_history:
            discrepancies.append("unacknowledged real broker order history during BROKER_SHADOW")
        if state_dirty:
            discrepancies.append("shadow ledger has an unpersisted mutation")
        if not observed.state_known:
            discrepancies.append("real broker account state is unknown")
        if not observed.is_authenticated:
            discrepancies.append("real broker account is not authenticated")
        if not self._firewall.healthcheck():
            discrepancies.append("deny-all external write firewall healthcheck failed")
        return ReconciliationReport(
            environment=ExecutionEnvironment.DEMO,
            reconciled_at=self._clock.now(),
            successful=not discrepancies,
            observed_account=observed,
            effective_account=effective,
            position_count=len(self._ledger.get_positions()),
            open_order_count=sum(
                order.state.value in {"OPEN", "PARTIAL"} for order in self._ledger.get_orders()
            ),
            discrepancies=tuple(discrepancies),
            details={
                "backend": "BROKER_SHADOW",
                "external_write_transport_present": False,
                "observed_position_count": len(observed_positions),
                "observed_order_count": len(observed_orders),
                "observed_active_order_count": len(observed_active_orders),
                "observed_historical_order_count": len(historical_orders),
                "unacknowledged_historical_order_count": len(unacknowledged_history),
                "shadow_state_persisted": not state_dirty,
                "fill_model_version": self._ledger.fill_model.version,
            },
        )
