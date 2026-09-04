from __future__ import annotations

from collections import defaultdict
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime, time
from typing import Any, Protocol
from uuid import UUID
from zoneinfo import ZoneInfo

from pydantic import BaseModel, ValidationError

from app.clock.base import Clock
from app.config import RuntimeBinding
from app.domain.enums import AccountKind, DemoBackend, ExecutionEnvironment, FirewallDisposition
from app.domain.models import (
    AccountSnapshot,
    BrokerCommandIntent,
    BrokerReview,
    FirewallDecision,
    OrderIntent,
    RiskDecision,
    TradeProposal,
)
from app.exceptions import SafetyCriticalError
from app.qualification.evaluator import (
    AuditCompleteness,
    QualificationEvaluator,
    QualificationReport,
    SessionEvidence,
)

_TABLE_LIMIT = 10_000
_NEW_YORK = ZoneInfo("America/New_York")


class QualificationRepository(Protocol):
    binding: RuntimeBinding

    async def append(self, table: str, value: BaseModel | Mapping[str, Any]) -> UUID: ...

    async def find_payload(self, table: str, key: str, value: Any) -> dict[str, Any] | None: ...

    async def list_payloads(
        self,
        table: str,
        *,
        filters: Mapping[str, Any] | None = None,
        limit: int = 1000,
        before_sequence: int | None = None,
    ) -> list[dict[str, Any]]: ...


class BrokerShadowQualificationService:
    """Build a qualification result from durable, current broker-shadow evidence."""

    def __init__(
        self,
        binding: RuntimeBinding,
        repository: QualificationRepository,
        clock: Clock,
        *,
        evaluator: QualificationEvaluator | None = None,
    ) -> None:
        if repository.binding != binding:
            raise ValueError("qualification repository must match the immutable runtime binding")
        if (
            binding.environment is not ExecutionEnvironment.DEMO
            or binding.demo_backend is not DemoBackend.BROKER_SHADOW
            or binding.external_write_authority
        ):
            raise ValueError("qualification service requires DEMO/BROKER_SHADOW with no writes")
        self.binding = binding
        self.repository = repository
        self.clock = clock
        self.evaluator = evaluator or QualificationEvaluator()

    async def record_session(self, evidence: SessionEvidence) -> QualificationReport:
        """Append one finalized session and a machine-readable evaluation snapshot."""

        if evidence.namespace != self.binding.idempotency_namespace:
            raise SafetyCriticalError("qualification session targets another namespace")
        existing, invalid = await self._load_sessions()
        if invalid:
            raise SafetyCriticalError("stored qualification evidence is invalid")
        if any(
            item.session_id == evidence.session_id
            or item.regular_session_date == evidence.regular_session_date
            for item in existing
        ):
            raise SafetyCriticalError("qualification session identity/date is already finalized")
        # Calendar and coverage errors make a purported finalized session unusable; do not
        # preserve it as if it were merely an in-progress observation.
        validation = self.evaluator.evaluate([evidence], self.clock.now())
        fatal = {
            "session_record_integrity",
            "full_regular_sessions",
            "authenticated_mcp_capabilities",
            "continuous_health_coverage",
            "real_account_unfunded",
            "real_shadow_account_separation",
            "reconciliation_and_incidents",
        }
        if fatal.intersection(validation.failed_gates):
            raise SafetyCriticalError("session cannot be finalized with failed evidence gates")
        await self.repository.append("qualification_session_summaries", evidence)
        report = await self.status()
        await self.repository.append(
            "qualification_runs",
            {
                "created_at": report.created_at,
                "environment": self.binding.environment.value,
                "namespace": self.binding.idempotency_namespace,
                "record_kind": "broker_shadow_qualification_evaluation",
                "report": report.model_dump(mode="json"),
            },
        )
        return report

    async def status(self) -> QualificationReport:
        sessions, invalid = await self._load_sessions()
        audit = await self._audit(sessions) if sessions else None
        return self.evaluator.evaluate(
            sessions,
            self.clock.now(),
            audit=audit,
            invalid_records=invalid,
        )

    async def _load_sessions(self) -> tuple[list[SessionEvidence], tuple[str, ...]]:
        rows = await self.repository.list_payloads(
            "qualification_session_summaries", limit=1000
        )
        invalid: list[str] = []
        if len(rows) == 1000:
            invalid.append("qualification session scan reached its safety bound")
        sessions: list[SessionEvidence] = []
        identities: set[UUID] = set()
        for row in rows:
            try:
                session = SessionEvidence.model_validate(_payload(row))
            except (ValidationError, TypeError, ValueError):
                invalid.append("stored qualification session failed schema validation")
                continue
            if session.namespace != self.binding.idempotency_namespace:
                invalid.append("stored qualification session has a foreign namespace")
                continue
            if session.session_id in identities:
                invalid.append("stored qualification session identity is duplicated")
                continue
            identities.add(session.session_id)
            sessions.append(session)
        return sessions, tuple(invalid)

    async def _audit(self, sessions: Sequence[SessionEvidence]) -> AuditCompleteness:
        issues: list[str] = []
        table_names = (
            "trade_proposals",
            "risk_decisions",
            "approvals",
            "broker_reviews",
            "order_intents",
            "broker_command_intents",
            "market_snapshots",
            "option_snapshots",
            "broker_capability_snapshots",
            "broker_observed_account_snapshots",
            "shadow_account_snapshots",
            "external_write_firewall_events",
            "demo_live_policy_divergences",
            "reconciliation_events",
            "environment_audit_events",
            "health_events",
        )
        tables: dict[str, list[dict[str, Any]]] = {}
        for table in table_names:
            rows = await self.repository.list_payloads(table, limit=_TABLE_LIMIT)
            tables[table] = rows
            if len(rows) == _TABLE_LIMIT:
                issues.append(f"{table} audit scan reached its safety bound")

        declared: dict[str, SessionEvidence] = {}
        natural: set[str] = set()
        plumbing: set[str] = set()
        for session in sessions:
            for value in session.natural_command_intent_ids:
                key = str(value)
                if key in declared:
                    issues.append("a command intent is declared by multiple qualification sessions")
                declared[key] = session
                natural.add(key)
            for value in session.plumbing_command_intent_ids:
                key = str(value)
                if key in declared:
                    issues.append("a command intent is declared by multiple qualification sessions")
                declared[key] = session
                plumbing.add(key)

        proposals = _index(tables["trade_proposals"], "proposal_id", issues)
        risks = _index(tables["risk_decisions"], "decision_id", issues)
        approvals = _index(tables["approvals"], "approval_id", issues)
        reviews = _index(tables["broker_reviews"], "review_id", issues)
        order_intents = _index(tables["order_intents"], "intent_id", issues)
        commands = _index(tables["broker_command_intents"], "command_intent_id", issues)
        market_snapshots = _index(tables["market_snapshots"], "snapshot_id", issues)
        for key, row in _index(tables["option_snapshots"], "snapshot_id", issues).items():
            if key in market_snapshots and market_snapshots[key] != row:
                issues.append("snapshot identity appears in both equity and option tables")
            market_snapshots[key] = row
        capabilities = _index(
            tables["broker_capability_snapshots"],
            "capability_snapshot_id",
            issues,
            include_row_id=True,
        )
        real_accounts = _index(
            tables["broker_observed_account_snapshots"], "snapshot_id", issues
        )
        shadow_accounts = _index(tables["shadow_account_snapshots"], "snapshot_id", issues)
        divergences = _index(
            tables["demo_live_policy_divergences"],
            "divergence_id",
            issues,
            include_row_id=True,
        )
        reconciliations = _index(
            tables["reconciliation_events"],
            "reconciliation_id",
            issues,
            include_row_id=True,
        )
        operational_events = _index(
            tables["environment_audit_events"] + tables["health_events"],
            "event_id",
            issues,
            include_row_id=True,
        )

        firewall_by_command: dict[str, list[dict[str, Any]]] = defaultdict(list)
        transmitted = 0
        for row in tables["external_write_firewall_events"]:
            payload = _payload(row)
            command_id = payload.get("command_intent_id")
            if command_id is not None:
                firewall_by_command[str(command_id)].append(row)
            if payload.get("transmitted") is True and self._in_any_session(row, sessions):
                transmitted += 1

        raw_in_period = {
            key for key, row in commands.items() if self._in_any_session(row, sessions)
        }
        undeclared = raw_in_period - set(declared)
        if undeclared:
            issues.append(
                f"{len(undeclared)} command intent(s) inside qualification windows are undeclared"
            )

        complete = 0
        safe_reviews: set[str] = set()
        denied_commands = 0
        account_separation_issues = 0
        real_account_breaches = 0

        for command_id, session in declared.items():
            command_row = commands.get(command_id)
            command_issues: list[str] = []
            if command_row is None:
                command_issues.append("command record missing")
                issues.append(f"command {command_id}: command record missing")
                continue
            try:
                command = BrokerCommandIntent.model_validate(_payload(command_row))
            except (ValidationError, TypeError, ValueError):
                issues.append(f"command {command_id}: command schema is invalid")
                continue
            if (
                command.environment is not ExecutionEnvironment.DEMO
                or command.namespace != self.binding.idempotency_namespace
            ):
                command_issues.append("runtime binding differs")
            if session.command_schema_hashes.get(command.capability_name) != (
                command.capability_schema_hash
            ):
                command_issues.append("capability schema hash is not in the session snapshot")
            if command_id in natural and not self._in_regular_window(command_row, session):
                command_issues.append("natural intent is outside the regular market window")
            if command_id in plumbing and not self._on_session_date(command_row, session):
                command_issues.append("plumbing-only intent is outside its labeled session date")

            order_intent = _validate_model(
                order_intents.get(str(command.order_intent_id)), OrderIntent
            )
            proposal = _validate_model(proposals.get(str(command.proposal_id)), TradeProposal)
            risk = _validate_model(risks.get(str(command.risk_decision_id)), RiskDecision)
            if order_intent is None:
                command_issues.append("durable OrderIntent link missing or invalid")
            else:
                linked_values = (
                    order_intent.proposal_id == command.proposal_id,
                    order_intent.risk_decision_id == command.risk_decision_id,
                    order_intent.approval_id == command.approval_id,
                    order_intent.order_fingerprint == command.order_fingerprint,
                    order_intent.idempotency_key == command.idempotency_key,
                    order_intent.action is command.action,
                )
                if not all(linked_values):
                    command_issues.append("BrokerCommandIntent differs from OrderIntent")
                review = _validate_model(
                    reviews.get(str(order_intent.review_id)), BrokerReview
                )
                if (
                    review is None
                    or review.proposal_id != command.proposal_id
                    or not review.side_effect_free
                ):
                    command_issues.append("safe broker review link missing or invalid")
                else:
                    safe_reviews.add(str(review.review_id))
            if proposal is None or proposal.quote_snapshot_id != command.quote_snapshot_id:
                command_issues.append("proposal/quote link missing or invalid")
            if risk is None or risk.proposal_id != command.proposal_id:
                command_issues.append("risk-decision link missing or invalid")
            if command.approval_id is not None and str(command.approval_id) not in approvals:
                command_issues.append("approval link missing")
            if str(command.quote_snapshot_id) not in market_snapshots:
                command_issues.append("market snapshot link missing")
            if (
                command.broker_observed_account_snapshot_id is None
                or str(command.broker_observed_account_snapshot_id) not in real_accounts
            ):
                command_issues.append("real broker account snapshot link missing")
            if str(command.effective_account_snapshot_id) not in shadow_accounts:
                command_issues.append("shadow account snapshot link missing")
            decisions = firewall_by_command.get(command_id, [])
            valid_denials = [
                decision
                for row in decisions
                if (decision := _validate_firewall_decision(row)) is not None
                and decision.disposition is FirewallDisposition.BLOCKED_SHADOW
                and not decision.transmitted
                and decision.environment is ExecutionEnvironment.DEMO
            ]
            if len(decisions) != 1 or len(valid_denials) != 1:
                command_issues.append(
                    "exactly one non-transmitted shadow firewall denial is required"
                )
            else:
                denied_commands += 1
            if command_issues:
                issues.extend(f"command {command_id}: {item}" for item in command_issues)
            else:
                complete += 1

        for session in sessions:
            capability = capabilities.get(str(session.capability_snapshot_id))
            capability_payload = _payload(capability) if capability is not None else {}
            if (
                capability is None
                or capability_payload.get("schema_hash") != session.capability_catalog_hash
                or capability_payload.get("version") != session.capability_catalog_version
                or capability_payload.get("external_write_authority") is not False
            ):
                issues.append(
                    f"session {session.regular_session_date}: "
                    "capability snapshot link is incomplete"
                )
            session_real_ids = {str(value) for value in session.real_broker_account_snapshot_ids}
            session_shadow_ids = {str(value) for value in session.shadow_account_snapshot_ids}
            if session_real_ids & session_shadow_ids:
                account_separation_issues += 1
                issues.append(
                    f"session {session.regular_session_date}: real and shadow snapshot IDs overlap"
                )
            for snapshot_id in session_real_ids:
                account = _validate_model(real_accounts.get(snapshot_id), AccountSnapshot)
                if (
                    account is None
                    or account.account_kind is not AccountKind.BROKER_OBSERVED
                    or account.account_fingerprint != session.account_fingerprint
                ):
                    account_separation_issues += 1
                    issues.append(
                        f"session {session.regular_session_date}: "
                        "real account snapshot is missing or mismatched"
                    )
                    continue
                if account.cash != 0 or account.buying_power != 0 or account.open_positions:
                    real_account_breaches += 1
            for snapshot_id in session_shadow_ids:
                account = _validate_model(shadow_accounts.get(snapshot_id), AccountSnapshot)
                if account is None or account.account_kind is not AccountKind.SHADOW:
                    account_separation_issues += 1
                    issues.append(
                        f"session {session.regular_session_date}: "
                        "shadow account snapshot is missing or mismatched"
                    )
            for divergence_id in session.policy_counterfactual_ids:
                if str(divergence_id) not in divergences:
                    issues.append(
                        f"session {session.regular_session_date}: "
                        "policy counterfactual record is missing"
                    )
            for reconciliation_id in session.reconciliation_event_ids:
                if str(reconciliation_id) not in reconciliations:
                    issues.append(
                        f"session {session.regular_session_date}: reconciliation record is missing"
                    )
            for event_id in session.restart_event_ids + session.transient_failure_event_ids:
                if str(event_id) not in operational_events:
                    issues.append(
                        f"session {session.regular_session_date}: "
                        "referenced operational event is missing"
                    )

        return AuditCompleteness(
            declared_command_intents=len(declared),
            natural_command_intents=len(natural),
            plumbing_command_intents=len(plumbing),
            complete_command_intents=complete,
            side_effect_free_reviews=len(safe_reviews),
            firewall_denials=denied_commands,
            transmitted_external_writes=transmitted,
            undeclared_command_intents=len(undeclared),
            real_account_breaches=real_account_breaches,
            account_separation_issues=account_separation_issues,
            issues=tuple(dict.fromkeys(issues)),
        )

    @staticmethod
    def _in_any_session(
        row: Mapping[str, Any], sessions: Sequence[SessionEvidence]
    ) -> bool:
        created_at = _row_created_at(row)
        return created_at is not None and any(
            session.premarket_observed_at <= created_at <= session.created_at
            for session in sessions
        )

    @staticmethod
    def _in_regular_window(row: Mapping[str, Any], session: SessionEvidence) -> bool:
        created_at = _row_created_at(row)
        opened = datetime.combine(
            session.regular_session_date, time(9, 30), tzinfo=_NEW_YORK
        ).astimezone(UTC)
        return (
            created_at is not None
            and opened <= created_at <= session.regular_market_close_observed_at
        )

    @staticmethod
    def _on_session_date(row: Mapping[str, Any], session: SessionEvidence) -> bool:
        created_at = _row_created_at(row)
        return (
            created_at is not None
            and created_at.astimezone(_NEW_YORK).date() == session.regular_session_date
        )


def _payload(row: Mapping[str, Any] | None) -> dict[str, Any]:
    if row is None:
        return {}
    payload = row.get("payload")
    if isinstance(payload, Mapping):
        return dict(payload)
    return dict(row)


def _index(
    rows: Sequence[dict[str, Any]],
    payload_key: str,
    issues: list[str],
    *,
    include_row_id: bool = False,
) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    for row in rows:
        payload = _payload(row)
        values: list[Any] = [payload.get(payload_key)]
        if include_row_id:
            values.append(row.get("id"))
        for value in values:
            if value is None:
                continue
            key = str(value)
            if key in result and result[key] != row:
                issues.append(f"duplicate immutable identity in {payload_key}")
            result[key] = row
    return result


def _validate_model[T: BaseModel](
    row: Mapping[str, Any] | None, model: type[T]
) -> T | None:
    if row is None:
        return None
    try:
        return model.model_validate(_payload(row))
    except (ValidationError, TypeError, ValueError):
        return None


def _row_created_at(row: Mapping[str, Any]) -> datetime | None:
    value = row.get("created_at", _payload(row).get("created_at"))
    if isinstance(value, datetime):
        result = value
    elif isinstance(value, str):
        try:
            result = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
    else:
        return None
    if result.tzinfo is None or result.utcoffset() is None:
        return None
    return result.astimezone(UTC)


def _validate_firewall_decision(row: Mapping[str, Any]) -> FirewallDecision | None:
    payload = _payload(row)
    fields = (
        "created_at",
        "firewall_event_id",
        "command_intent_id",
        "environment",
        "disposition",
        "reason",
        "transmitted",
    )
    if any(field not in payload for field in fields):
        return None
    try:
        return FirewallDecision.model_validate({field: payload[field] for field in fields})
    except (ValidationError, TypeError, ValueError):
        return None
