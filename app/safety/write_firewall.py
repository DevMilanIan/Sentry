from __future__ import annotations

import inspect
from collections.abc import Awaitable, Callable

from app.clock.base import Clock
from app.domain.enums import ExecutionEnvironment, FirewallDisposition
from app.domain.models import BrokerCommandIntent, FirewallDecision
from app.exceptions import SafetyCriticalError

FirewallRecorder = Callable[[FirewallDecision], Awaitable[None] | None]


class DenyAllWriteFirewall:
    """Terminal shadow boundary deliberately containing no network transport."""

    mode = "DENY_ALL_WRITES"

    def __init__(self, clock: Clock, recorder: FirewallRecorder | None = None) -> None:
        self._clock = clock
        self._recorder = recorder
        self._healthy = True

    def healthcheck(self) -> bool:
        return self._healthy and not hasattr(self, "transport")

    async def evaluate(self, command: BrokerCommandIntent) -> FirewallDecision:
        if command.environment is not ExecutionEnvironment.DEMO:
            raise SafetyCriticalError("deny-all shadow firewall received non-DEMO command")
        decision = FirewallDecision(
            created_at=self._clock.now(),
            command_intent_id=command.command_intent_id,
            environment=command.environment,
            disposition=FirewallDisposition.BLOCKED_SHADOW,
            reason="BROKER_SHADOW structurally denies every external mutation",
            transmitted=False,
        )
        if self._recorder:
            recorded = self._recorder(decision)
            if inspect.isawaitable(recorded):
                await recorded
        return decision


class LiveWriteAuthorizer:
    """Last in-process policy check; actual Live transport is separately injected."""

    def __init__(
        self, clock: Clock, *, explicitly_authorized: bool, recorder: FirewallRecorder | None = None
    ) -> None:
        self._clock = clock
        self._explicitly_authorized = explicitly_authorized
        self._recorder = recorder

    async def evaluate(self, command: BrokerCommandIntent) -> FirewallDecision:
        if command.environment is not ExecutionEnvironment.LIVE or not self._explicitly_authorized:
            decision = FirewallDecision(
                created_at=self._clock.now(),
                command_intent_id=command.command_intent_id,
                environment=command.environment,
                disposition=FirewallDisposition.BLOCKED_POLICY,
                reason="Live write authority is absent",
                transmitted=False,
            )
        else:
            decision = FirewallDecision(
                created_at=self._clock.now(),
                command_intent_id=command.command_intent_id,
                environment=command.environment,
                disposition=FirewallDisposition.AUTHORIZED_LIVE,
                reason="explicit Live authority present; transport still performs final gates",
                transmitted=False,
            )
        if self._recorder:
            recorded = self._recorder(decision)
            if inspect.isawaitable(recorded):
                await recorded
        return decision
