from __future__ import annotations

import asyncio
from datetime import date, timedelta
from decimal import Decimal
from uuid import UUID

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
    ShadowLedger,
)
from app.clock.base import Clock
from app.domain.enums import AccountKind, BrokerAction, ExecutionEnvironment, FirewallDisposition
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
)
from app.exceptions import SafetyCriticalError, SentinelError
from app.safety.write_firewall import DenyAllWriteFirewall


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
    ) -> None:
        super().__init__(command_recorder=command_recorder)
        selected_firewall = firewall or DenyAllWriteFirewall(clock)
        if not isinstance(selected_firewall, DenyAllWriteFirewall):
            raise SafetyCriticalError("broker-shadow requires the deny-all write firewall")
        if meaningful_external_balance < 0:
            raise ValueError("meaningful external balance threshold cannot be negative")
        self._clock = clock
        self._read_client = read_client
        self._firewall = selected_firewall
        self._meaningful_external_balance = meaningful_external_balance
        self._ledger = ShadowLedger(
            clock=clock,
            initial_cash=initial_cash,
            account_kind=AccountKind.SHADOW,
            fill_model=fill_model or ConservativeFillModel(seed_salt=fill_seed),
            max_quote_age=max_quote_age,
            namespace=namespace,
        )
        self._lock = asyncio.Lock()
        self._capabilities: BrokerCapabilities | None = None
        self._last_broker_review: BrokerReview | None = None
        self._last_shadow_review: BrokerReview | None = None

    @property
    def ledger(self) -> ShadowLedger:
        return self._ledger

    @property
    def last_broker_observed_review(self) -> BrokerReview | None:
        return self._last_broker_review

    @property
    def last_shadow_execution_review(self) -> BrokerReview | None:
        return self._last_shadow_review

    async def get_capabilities(self) -> BrokerCapabilities:
        discovered = await self._read_client.get_capabilities()
        if self._capabilities is None:
            issues = list(discovered.issues)
            if not self._firewall.healthcheck():
                issues.append("deny-all external write firewall is unhealthy")
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
            shadow_review = self._ledger.review(proposal)
            self._last_shadow_review = shadow_review

        broker_review: BrokerReview | None = None
        broker_error: str | None = None
        try:
            broker_review = await self._read_client.review_option_order(proposal)
        except SentinelError as exc:
            # Read/review connectivity is measured separately; it never changes
            # the real-write denial.  The combined review records the outage.
            broker_error = f"{type(exc).__name__}: {exc}"
        self._last_broker_review = broker_review

        warnings = [f"shadow: {item}" for item in shadow_review.warnings]
        if broker_review is not None:
            warnings.extend(f"broker-observed: {item}" for item in broker_review.warnings)
            if not broker_review.accepted:
                warnings.append(
                    "broker-observed review rejected; shadow acceptance remains separate"
                )
        elif broker_error is not None:
            warnings.append(f"broker-observed review unavailable: {broker_error}")
        reference = (
            f"shadow={shadow_review.review_id};broker={broker_review.review_id}"
            if broker_review is not None
            else f"shadow={shadow_review.review_id};broker=unavailable"
        )
        return BrokerReview(
            created_at=self._clock.now(),
            environment=ExecutionEnvironment.DEMO,
            proposal_id=proposal.proposal_id,
            accepted=shadow_review.accepted,
            warnings=tuple(warnings),
            raw_reference=reference,
            side_effect_free=True,
        )

    async def place_option_order(
        self,
        command: BrokerCommandIntent,
        contract: OptionContract,
    ) -> BrokerOrder:
        if command.action is not BrokerAction.PLACE_OPTION_ORDER:
            raise SafetyCriticalError("shadow placement received a non-placement command")
        await self._read_client.validate_command(command)
        await self.record_broker_command_intent(command)
        decision = await self._firewall.evaluate(command)
        if decision.transmitted or decision.disposition is not FirewallDisposition.BLOCKED_SHADOW:
            raise SafetyCriticalError("broker-shadow write firewall did not deny placement")
        async with self._lock:
            return self._ledger.submit(command, contract)

    async def cancel_option_order(
        self,
        command: BrokerCommandIntent,
        order_id: UUID | str,
    ) -> BrokerOrder:
        if command.action is not BrokerAction.CANCEL_OPTION_ORDER:
            raise SafetyCriticalError("shadow cancellation received a non-cancel command")
        await self._read_client.validate_command(command)
        await self.record_broker_command_intent(command)
        decision = await self._firewall.evaluate(command)
        if decision.transmitted or decision.disposition is not FirewallDisposition.BLOCKED_SHADOW:
            raise SafetyCriticalError("broker-shadow write firewall did not deny cancellation")
        async with self._lock:
            return self._ledger.cancel(command, order_id)

    async def consume_quote(self, quote: OptionQuote) -> tuple[Fill, ...]:
        async with self._lock:
            return self._ledger.observe_quote(quote)

    async def deposit(
        self,
        amount: Decimal,
        *,
        reference: str = "configured-shadow-scenario",
    ) -> DepositRecord:
        async with self._lock:
            return self._ledger.deposit(amount, reference=reference)

    async def process_expirations(
        self,
        *,
        on_date: date | None = None,
        policy: ExpirationPolicy = ExpirationPolicy.DO_NOT_EXERCISE,
        cash_settlement_per_share: dict[str, Decimal] | None = None,
    ) -> ExpirationResult:
        async with self._lock:
            return self._ledger.expire(
                on_date=on_date,
                policy=policy,
                cash_settlement_per_share=cash_settlement_per_share,
            )

    async def reconcile(self) -> ReconciliationReport:
        observed, observed_positions, observed_orders = await asyncio.gather(
            self.get_observed_broker_account_state(),
            self.get_observed_broker_positions(),
            self.get_observed_broker_orders(),
        )
        async with self._lock:
            local = self._ledger.reconciliation_report()
            effective = local.effective_account
        discrepancies = list(local.discrepancies)
        threshold = self._meaningful_external_balance
        if observed.cash > threshold:
            discrepancies.append("unexpected real broker cash during BROKER_SHADOW")
        if observed.buying_power > threshold:
            discrepancies.append("unexpected real broker buying power during BROKER_SHADOW")
        if observed_positions:
            discrepancies.append("unexpected real broker option position during BROKER_SHADOW")
        if observed_orders:
            discrepancies.append("unexpected real broker option order during BROKER_SHADOW")
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
                "fill_model_version": self._ledger.fill_model.version,
            },
        )
