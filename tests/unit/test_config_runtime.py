from __future__ import annotations

from copy import deepcopy
from pathlib import Path

import pytest
import yaml

from app.config import AppConfig, DemoProfile, SourcesConfig, load_config
from app.domain.enums import ExecutionEnvironment, TradingMode


def test_checked_in_config_binds_safe_demo(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in (
        "SENTRY_EXECUTION_ENVIRONMENT",
        "SENTRY_DEMO_BACKEND",
        "SENTRY_TRADING_MODE",
        "SENTRY_DATABASE_URL",
        "SENTRY_OLLAMA_MODEL",
        "SENTRY_OLLAMA_URL",
    ):
        monkeypatch.delenv(name, raising=False)
    loaded = load_config(Path("config"))
    binding = loaded.bind_runtime()
    assert binding.environment is ExecutionEnvironment.DEMO
    assert binding.external_write_authority is False
    assert loaded.app.trading_mode is TradingMode.RESEARCH
    assert loaded.model_provider.model == "qwen3.5:4b"
    assert loaded.model_provider.context_token_limit == 4096
    assert loaded.strategy.options.maximum_candidates_for_judge == 3
    assert len(loaded.sources.official_sources) >= 6


def test_demo_profile_rejects_external_write_authority() -> None:
    raw = yaml.safe_load(Path("config/demo.yaml").read_text(encoding="utf-8"))
    raw["external_write_authority"] = True
    with pytest.raises(ValueError, match="never have external write authority"):
        DemoProfile.model_validate(raw)


def test_live_shadow_combination_is_rejected() -> None:
    raw = yaml.safe_load(Path("config/app.yaml").read_text(encoding="utf-8"))
    raw = deepcopy(raw)
    raw["execution_environment"] = "LIVE"
    raw["demo_backend"] = None
    raw["trading_mode"] = "SHADOW"
    with pytest.raises(ValueError, match=r"LIVE\+SHADOW"):
        AppConfig.model_validate(raw)


def test_enabled_official_sources_match_verified_public_feed_checkpoint() -> None:
    sources = SourcesConfig.model_validate(
        yaml.safe_load(Path("config/sources.yaml").read_text(encoding="utf-8"))
    )
    assert sources.version == "sources-v4-explicit-issuer-mapping"
    assert sources.issuer_mappings == ()
    assert {source.id: source.url for source in sources.official_sources if source.enabled} == {
        "federal_reserve": "https://www.federalreserve.gov/feeds/press_all.xml",
        "ftc": "https://www.ftc.gov/feeds/press-release.xml",
        "eia_today_in_energy": "https://www.eia.gov/rss/todayinenergy.xml",
        "eia_releases": "https://www.eia.gov/about/new/WNtest3.php",
    }
    assert sources.poll_seconds >= 900  # Exceeds FTC's observed five-second crawl delay.
    assert not next(source for source in sources.official_sources if source.id == "sec").enabled
