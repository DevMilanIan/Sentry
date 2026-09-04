from datetime import UTC, datetime

from fastapi.testclient import TestClient

from app.clock.base import VirtualClock
from app.config import load_config
from app.db.repository import InMemoryAuditRepository
from app.demo.offline_scenario import _scripted_outputs
from app.reasoning.scripted import ScriptedReplayModelProvider
from app.runtime import build_application


async def test_runtime_registry_is_token_protected_and_uses_wall_not_replay_time() -> None:
    loaded = load_config()
    loaded = loaded.model_copy(
        update={
            "app": loaded.app.model_copy(
                update={
                    "dashboard": loaded.app.dashboard.model_copy(
                        update={"require_token_for_controls": False}
                    )
                }
            )
        }
    )
    wall = VirtualClock(datetime(2026, 9, 4, tzinfo=UTC))
    runtime = await build_application(
        loaded,
        repository=InMemoryAuditRepository(loaded.bind_runtime()),
        model_provider=ScriptedReplayModelProvider(_scripted_outputs()),
        wall_clock=wall,
        dashboard_token="test-reference-token",
    )
    try:
        # No lifespan/controller loops needed to inspect the composed routes.
        client = TestClient(runtime.application)
        assert client.get("/api/federal/relationships").status_code == 401
        response = client.get(
            "/api/federal/relationships",
            headers={"X-Dashboard-Token": "test-reference-token"},
        )
        assert response.status_code == 200
        assert response.json()["as_of"] == "2026-09-04T00:00:00Z"
        assert response.json()["items"] == []
        assert runtime.offline is not None
        assert runtime.offline.clock.now().year == 2026
        assert runtime.offline.clock.now().month == 1
    finally:
        await runtime.close()
