from __future__ import annotations

from datetime import timedelta

from fastapi.testclient import TestClient

from app.api.dashboard import RuntimeView, create_app
from app.config import AppConfig, RuntimeBinding, load_config
from app.db.repository import InMemoryAuditRepository
from app.domain.enums import RuntimeSafetyState
from app.observability.metrics import MetricsRegistry
from app.safety.runtime_state import SafetyController


def test_controls_require_token_and_emergency_stop_is_audited(
    clock, demo_binding: RuntimeBinding, tmp_path
) -> None:
    loaded = load_config().app
    config = AppConfig.model_validate(
        {
            **loaded.model_dump(mode="python"),
            "runtime": {
                **loaded.runtime.model_dump(mode="python"),
                "disabled_file": tmp_path / "TRADING_DISABLED",
            },
        }
    )
    repository = InMemoryAuditRepository(demo_binding)
    safety = SafetyController(clock, timedelta(seconds=30))
    view = RuntimeView(binding=demo_binding, trading_mode=config.trading_mode, safety=safety)
    application = create_app(
        config, view, repository, clock, MetricsRegistry(), dashboard_token="test-token"
    )
    with TestClient(application) as client:
        denied = client.post("/api/control/emergency-stop")
        assert denied.status_code == 401
        accepted = client.post(
            "/api/control/emergency-stop", headers={"X-Dashboard-Token": "test-token"}
        )
        assert accepted.status_code == 200
        assert accepted.json()["state"] == RuntimeSafetyState.HALTED.value
        assert config.runtime.disabled_file.is_file()
