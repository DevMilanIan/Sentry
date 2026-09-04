from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta

from app.clock.base import Clock
from app.domain.enums import RuntimeSafetyState


@dataclass(frozen=True, slots=True)
class SafetyEvidence:
    database_writable: bool
    broker_state_known: bool
    reconciled: bool
    market_data_fresh: bool
    account_data_fresh: bool
    execution_service_healthy: bool
    kill_switch_clear: bool
    environment_matches: bool
    unresolved_submission: bool = False

    @property
    def permits_normal(self) -> bool:
        return all(
            (
                self.database_writable,
                self.broker_state_known,
                self.reconciled,
                self.market_data_fresh,
                self.account_data_fresh,
                self.execution_service_healthy,
                self.kill_switch_clear,
                self.environment_matches,
                not self.unresolved_submission,
            )
        )


class SafetyController:
    """Central fail-closed state machine; model code receives no reference to it."""

    _rank = {
        RuntimeSafetyState.NORMAL: 0,
        RuntimeSafetyState.ENTRY_DISABLED: 1,
        RuntimeSafetyState.EXIT_ONLY: 2,
        RuntimeSafetyState.HALTED: 3,
    }

    def __init__(self, clock: Clock, startup_health_window: timedelta) -> None:
        self._clock = clock
        self._state = RuntimeSafetyState.ENTRY_DISABLED
        self._reason = "startup reconciliation and health window required"
        self._healthy_since: float | None = None
        self._window = startup_health_window
        self._entries_paused = False

    @property
    def state(self) -> RuntimeSafetyState:
        return self._state

    @property
    def reason(self) -> str:
        return self._reason

    def degrade(self, target: RuntimeSafetyState, reason: str) -> None:
        if target is RuntimeSafetyState.NORMAL:
            raise ValueError("degrade cannot request NORMAL")
        if self._rank[target] >= self._rank[self._state]:
            self._state = target
            self._reason = reason
            self._healthy_since = None

    def emergency_stop(self, reason: str = "emergency stop") -> None:
        self._state = RuntimeSafetyState.HALTED
        self._reason = reason
        self._healthy_since = None

    def pause_entries(self, reason: str = "entries paused by operator") -> None:
        self._entries_paused = True
        self.degrade(RuntimeSafetyState.ENTRY_DISABLED, reason)

    def resume_entries(
        self,
        evidence: SafetyEvidence,
        *,
        manual_halt_cleared: bool = False,
    ) -> None:
        self._entries_paused = False
        self.observe(evidence, manual_halt_cleared=manual_halt_cleared)

    def observe(self, evidence: SafetyEvidence, *, manual_halt_cleared: bool = False) -> None:
        # NTP/manual wall-clock changes cannot accelerate the real health window.
        # Virtual clocks advance this value causally with replay time.
        now = self._clock.elapsed_seconds()
        if not evidence.kill_switch_clear:
            self.emergency_stop("kill switch active")
            return
        if evidence.unresolved_submission or not evidence.database_writable:
            self.emergency_stop("unresolved submission or database unavailable")
            return
        if not evidence.broker_state_known or not evidence.environment_matches:
            self.degrade(RuntimeSafetyState.ENTRY_DISABLED, "broker/environment state unknown")
            return
        if self._entries_paused:
            self.degrade(RuntimeSafetyState.ENTRY_DISABLED, "entries paused by operator")
            return
        if not evidence.permits_normal:
            self.degrade(RuntimeSafetyState.ENTRY_DISABLED, "health evidence incomplete")
            return
        if self._state is RuntimeSafetyState.HALTED and not manual_halt_cleared:
            return
        if self._healthy_since is None:
            self._healthy_since = now
            self._state = RuntimeSafetyState.ENTRY_DISABLED
            self._reason = "health window accumulating"
            return
        if now < self._healthy_since:
            self.degrade(RuntimeSafetyState.ENTRY_DISABLED, "elapsed clock moved backward")
            return
        if now - self._healthy_since >= self._window.total_seconds():
            self._state = RuntimeSafetyState.NORMAL
            self._reason = "all deterministic health gates passed"

    def permits_new_entry(self) -> bool:
        return self._state is RuntimeSafetyState.NORMAL

    def permits_risk_reducing_exit(self) -> bool:
        return self._state in {
            RuntimeSafetyState.NORMAL,
            RuntimeSafetyState.ENTRY_DISABLED,
            RuntimeSafetyState.EXIT_ONLY,
        }
