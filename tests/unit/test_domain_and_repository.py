from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from app.config import RuntimeBinding
from app.db.repository import InMemoryAuditRepository
from app.domain.enums import ExecutionEnvironment
from app.domain.models import ExactApproval, TradeProposal
from app.exceptions import SafetyCriticalError


def test_approval_is_exact_environment_and_fingerprint(
    proposal: TradeProposal, instant: datetime
) -> None:
    approval = ExactApproval(
        created_at=instant,
        environment=proposal.environment,
        namespace=proposal.namespace,
        proposal_id=proposal.proposal_id,
        order_fingerprint=proposal.order_fingerprint,
        maximum_limit_price=Decimal("0.09"),
        expires_at=instant + timedelta(minutes=5),
        approved_by="tester",
    )
    assert approval.is_valid_for(proposal, instant)
    changed = proposal.model_copy(update={"limit_price": Decimal("0.10")})
    assert not approval.is_valid_for(changed, instant)


@pytest.mark.asyncio
async def test_environment_repository_rejects_live_record(
    demo_binding: RuntimeBinding, instant: datetime
) -> None:
    repository = InMemoryAuditRepository(demo_binding)
    with pytest.raises(SafetyCriticalError, match="cross-environment"):
        await repository.append(
            "orders",
            {
                "created_at": instant,
                "environment": ExecutionEnvironment.LIVE.value,
                "state": "OPEN",
            },
        )


@pytest.mark.asyncio
async def test_separate_repositories_cannot_observe_each_other(
    demo_binding: RuntimeBinding, instant: datetime
) -> None:
    demo = InMemoryAuditRepository(demo_binding)
    live_binding = RuntimeBinding(
        environment=ExecutionEnvironment.LIVE,
        demo_backend=None,
        database_schema="live",
        runtime_directory=demo_binding.runtime_directory.parent / "live",
        idempotency_namespace="live-test",
        external_write_authority=False,
        config_version="live-test",
    )
    live = InMemoryAuditRepository(live_binding)
    await demo.append(
        "approvals", {"created_at": instant, "environment": "DEMO", "approval": "fixture"}
    )
    assert len(await demo.list("approvals")) == 1
    assert await live.list("approvals") == []


@pytest.mark.asyncio
async def test_mapping_payload_is_recursively_json_normalized(
    demo_binding: RuntimeBinding, instant: datetime
) -> None:
    repository = InMemoryAuditRepository(demo_binding)
    row_id = await repository.append(
        "health_events",
        {
            "created_at": instant,
            "environment": ExecutionEnvironment.DEMO,
            "nested": {
                "observed_at": instant,
                "amount": Decimal("1.25"),
            },
        },
    )

    rows = await repository.list("health_events")
    assert rows[0]["id"] == row_id
    assert rows[0]["payload"] == {
        "created_at": instant.astimezone(UTC).isoformat().replace("+00:00", "Z"),
        "environment": "DEMO",
        "nested": {
            "observed_at": instant.astimezone(UTC).isoformat().replace("+00:00", "Z"),
            "amount": "1.25",
        },
    }
