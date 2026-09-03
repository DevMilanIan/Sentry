from __future__ import annotations

import hashlib
import os
from decimal import Decimal
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.domain.enums import DemoBackend, ExecutionEnvironment, RuntimeSafetyState, TradingMode
from app.exceptions import ConfigurationError
from app.reasoning.policies import DecisionPolicySet
from app.reasoning.provider import ModelProviderConfig


class ConfigModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class DatabaseConfig(ConfigModel):
    url: str
    shared_schema: str = "shared"
    demo_schema: str = "demo"
    live_schema: str = "live"


class RuntimeConfig(ConfigModel):
    instance_lock_dir: Path = Path("var/locks")
    disabled_file: Path = Path("TRADING_DISABLED")
    environment_execution_disabled: bool = True
    startup_health_window_seconds: int = Field(default=30, ge=0)
    stale_market_data_seconds: int = Field(default=120, gt=0)
    stale_account_data_seconds: int = Field(default=60, gt=0)
    offline_fixture: Path = Path("app/market/fixtures/offline_e2e_session.json")
    offline_step_seconds: int = Field(default=5, gt=0)


class DashboardConfig(ConfigModel):
    host: str = "127.0.0.1"
    port: int = Field(default=8000, ge=1, le=65535)
    require_token_for_controls: bool = True


class SentinelCadenceConfig(ConfigModel):
    health_seconds: int = Field(default=30, gt=0)
    positions_seconds: int = Field(default=60, gt=0)
    equity_scan_seconds: int = Field(default=300, gt=0)
    option_refresh_seconds: int = Field(default=600, gt=0)
    catalyst_seconds: int = Field(default=900, gt=0)
    research_seconds: int = Field(default=1200, gt=0)


class AppConfig(ConfigModel):
    version: str
    execution_environment: ExecutionEnvironment
    demo_backend: DemoBackend | None = None
    trading_mode: TradingMode
    timezone: str = "America/New_York"
    database: DatabaseConfig
    runtime: RuntimeConfig
    dashboard: DashboardConfig
    sentinel: SentinelCadenceConfig

    @model_validator(mode="after")
    def environment_backend_pair(self) -> AppConfig:
        if self.execution_environment is ExecutionEnvironment.DEMO and self.demo_backend is None:
            raise ValueError("DEMO requires a startup demo_backend")
        if (
            self.execution_environment is ExecutionEnvironment.LIVE
            and self.demo_backend is not None
        ):
            raise ValueError("LIVE cannot configure a Demo backend")
        if (
            self.execution_environment is ExecutionEnvironment.LIVE
            and self.trading_mode is TradingMode.SHADOW
        ):
            raise ValueError("LIVE+SHADOW is undefined; use LIVE+RESEARCH")
        return self


class PortfolioRiskConfig(ConfigModel):
    starting_capital_reference: Decimal = Field(gt=0)


class FundingConfig(ConfigModel):
    expected_weekly_min: Decimal = Field(ge=0)
    expected_weekly_max: Decimal = Field(ge=0)

    @model_validator(mode="after")
    def ordered_range(self) -> FundingConfig:
        if self.expected_weekly_min > self.expected_weekly_max:
            raise ValueError("weekly funding minimum exceeds maximum")
        return self


class StrategyRiskConfig(ConfigModel):
    allowed_instruments: tuple[str, ...]
    maximum_contracts_per_position: int = Field(gt=0)


class LimitsConfig(ConfigModel):
    max_new_trade_premium_dollars: Decimal = Field(gt=0)
    max_total_open_option_risk_dollars: Decimal = Field(gt=0)
    max_open_positions: int = Field(gt=0)
    max_new_entries_per_day: int = Field(gt=0)
    daily_new_entries_enabled: bool = True


class LiquidityConfig(ConfigModel):
    require_nonzero_bid: bool = True
    starting_minimum_open_interest: int = Field(ge=0)
    starting_minimum_option_volume: int = Field(ge=0)
    starting_maximum_bid_ask_percent: Decimal = Field(gt=0)


class ExecutionRulesConfig(ConfigModel):
    limit_orders_only: bool = True
    market_orders_allowed: bool = False
    pretrade_review_required: bool = True
    human_entry_approval_required: bool = True

    @model_validator(mode="after")
    def no_market_contradiction(self) -> ExecutionRulesConfig:
        if self.limit_orders_only and self.market_orders_allowed:
            raise ValueError("market orders cannot be allowed when limit-only is enabled")
        return self


class ProhibitedConfig(ConfigModel):
    naked_options: bool = True
    short_options: bool = True
    borrowed_margin: bool = True
    martingale: bool = True
    automatic_risk_limit_changes: bool = True

    @model_validator(mode="after")
    def all_prohibitions_enabled(self) -> ProhibitedConfig:
        if not all(self.model_dump().values()):
            raise ValueError("V1 prohibited behavior flags must all remain true")
        return self


class RiskConfig(ConfigModel):
    version: str
    portfolio: PortfolioRiskConfig
    funding: FundingConfig
    strategy: StrategyRiskConfig
    risk: LimitsConfig
    liquidity: LiquidityConfig
    execution: ExecutionRulesConfig
    prohibited: ProhibitedConfig


class AttentionThresholdConfig(ConfigModel):
    l1_watch: Decimal = Field(ge=0, le=100)
    l2_candidate: Decimal = Field(ge=0, le=100)
    l3_trade_worthy: Decimal = Field(ge=0, le=100)
    l4_deep_research: Decimal = Field(ge=0, le=100)

    @model_validator(mode="after")
    def ordered(self) -> AttentionThresholdConfig:
        if not (
            self.l1_watch <= self.l2_candidate <= self.l3_trade_worthy <= self.l4_deep_research
        ):
            raise ValueError("strategy attention thresholds must be ordered")
        return self


class OptionSelectionConfig(ConfigModel):
    minimum_dte: int = Field(ge=0)
    maximum_dte: int = Field(ge=0)
    maximum_absolute_moneyness_percent: Decimal = Field(ge=0)
    maximum_candidates_for_judge: int = Field(ge=1, le=20)

    @model_validator(mode="after")
    def ordered(self) -> OptionSelectionConfig:
        if self.minimum_dte > self.maximum_dte:
            raise ValueError("strategy minimum_dte exceeds maximum_dte")
        return self


class ScoringConfig(ConfigModel):
    surveillance: dict[str, Decimal]
    trade_quality: dict[str, Decimal]

    @model_validator(mode="after")
    def positive_weights(self) -> ScoringConfig:
        if not self.surveillance or not self.trade_quality:
            raise ValueError("strategy scoring maps cannot be empty")
        if any(value <= 0 for value in (*self.surveillance.values(), *self.trade_quality.values())):
            raise ValueError("strategy scoring weights must be positive")
        return self


class StrategyConfig(ConfigModel):
    version: str
    attention_thresholds: AttentionThresholdConfig
    options: OptionSelectionConfig
    scoring: ScoringConfig


class OfficialSourceConfig(ConfigModel):
    id: str = Field(min_length=1)
    url: str = Field(pattern=r"^https://")


class SourcesConfig(ConfigModel):
    version: str
    sec_user_agent: str = Field(min_length=10)
    poll_seconds: int = Field(gt=0)
    official_sources: tuple[OfficialSourceConfig, ...]

    @model_validator(mode="after")
    def unique_source_ids(self) -> SourcesConfig:
        ids = [source.id for source in self.official_sources]
        if len(ids) != len(set(ids)):
            raise ValueError("official source IDs must be unique")
        return self


class DemoProfile(ConfigModel):
    version: str
    execution_environment: ExecutionEnvironment
    demo_backend: DemoBackend
    database_schema: str
    runtime_directory: Path
    idempotency_namespace: str
    initial_cash: Decimal = Field(gt=0)
    fill_model: str
    fill_seed: int
    external_write_authority: bool = False

    @model_validator(mode="after")
    def demo_is_never_live_authority(self) -> DemoProfile:
        if self.execution_environment is not ExecutionEnvironment.DEMO:
            raise ValueError("Demo profile must bind DEMO")
        if self.external_write_authority:
            raise ValueError("Demo profile can never have external write authority")
        return self


class LiveProfile(ConfigModel):
    version: str
    execution_environment: ExecutionEnvironment
    database_schema: str
    runtime_directory: Path
    idempotency_namespace: str
    external_write_authority: bool = False
    requires_authorization_file: bool = True
    requires_qualified_account_fingerprint: bool = True
    requires_zero_open_gates: bool = True
    startup_safety_state: RuntimeSafetyState = RuntimeSafetyState.HALTED
    decision_policy: str = "LIVE_CONSERVATIVE"

    @model_validator(mode="after")
    def locked_defaults(self) -> LiveProfile:
        if self.execution_environment is not ExecutionEnvironment.LIVE:
            raise ValueError("Live profile must bind LIVE")
        if self.startup_safety_state is not RuntimeSafetyState.HALTED:
            raise ValueError("Live must start HALTED")
        return self


class RuntimeBinding(ConfigModel):
    environment: ExecutionEnvironment
    demo_backend: DemoBackend | None
    database_schema: str
    runtime_directory: Path
    idempotency_namespace: str
    external_write_authority: bool
    config_version: str


class LoadedConfig(ConfigModel):
    app: AppConfig
    risk: RiskConfig
    strategy: StrategyConfig
    sources: SourcesConfig
    model_provider: ModelProviderConfig
    decision_policies: DecisionPolicySet
    demo: DemoProfile
    broker_shadow: DemoProfile
    live: LiveProfile
    source_hashes: dict[str, str]

    def bind_runtime(self) -> RuntimeBinding:
        if self.app.execution_environment is ExecutionEnvironment.DEMO:
            profile = (
                self.demo
                if self.app.demo_backend is DemoBackend.OFFLINE_SIM
                else self.broker_shadow
            )
            if self.app.demo_backend is not profile.demo_backend:
                raise ConfigurationError("app and Demo profile backend mismatch")
            return RuntimeBinding(
                environment=ExecutionEnvironment.DEMO,
                demo_backend=profile.demo_backend,
                database_schema=profile.database_schema,
                runtime_directory=profile.runtime_directory,
                idempotency_namespace=profile.idempotency_namespace,
                external_write_authority=False,
                config_version=profile.version,
            )
        return RuntimeBinding(
            environment=ExecutionEnvironment.LIVE,
            demo_backend=None,
            database_schema=self.live.database_schema,
            runtime_directory=self.live.runtime_directory,
            idempotency_namespace=self.live.idempotency_namespace,
            external_write_authority=self.live.external_write_authority,
            config_version=self.live.version,
        )


def _read_yaml(path: Path) -> tuple[dict[str, Any], str]:
    raw = path.read_bytes()
    parsed = yaml.safe_load(raw) or {}
    if not isinstance(parsed, dict):
        raise ConfigurationError(f"{path} must contain a YAML mapping")
    return parsed, hashlib.sha256(raw).hexdigest()


def load_config(config_dir: Path = Path("config")) -> LoadedConfig:
    documents: dict[str, tuple[dict[str, Any], str]] = {}
    for name in (
        "app",
        "risk",
        "strategy",
        "sources",
        "model",
        "demo",
        "broker_shadow",
        "live",
        "decision_policies",
    ):
        path = config_dir / f"{name}.yaml"
        if not path.is_file():
            raise ConfigurationError(f"missing required config: {path}")
        documents[name] = _read_yaml(path)

    app_data = dict(documents["app"][0])
    model_data = dict(documents["model"][0])
    env_name = os.getenv("SENTRY_EXECUTION_ENVIRONMENT")
    backend_name = os.getenv("SENTRY_DEMO_BACKEND")
    mode_name = os.getenv("SENTRY_TRADING_MODE")
    database_url = os.getenv("SENTRY_DATABASE_URL")
    ollama_url = os.getenv("SENTRY_OLLAMA_URL")
    ollama_model = os.getenv("SENTRY_OLLAMA_MODEL")
    if env_name:
        app_data["execution_environment"] = env_name
        if env_name == ExecutionEnvironment.LIVE.value:
            app_data["demo_backend"] = None
    if backend_name and app_data.get("execution_environment") == ExecutionEnvironment.DEMO.value:
        app_data["demo_backend"] = backend_name
    if mode_name:
        app_data["trading_mode"] = mode_name
    if database_url:
        app_data.setdefault("database", {})["url"] = database_url
    if ollama_url:
        model_data["base_url"] = ollama_url
    if ollama_model:
        model_data["model"] = ollama_model

    try:
        return LoadedConfig(
            app=AppConfig.model_validate(app_data),
            risk=RiskConfig.model_validate(documents["risk"][0]),
            strategy=StrategyConfig.model_validate(documents["strategy"][0]),
            sources=SourcesConfig.model_validate(documents["sources"][0]),
            model_provider=ModelProviderConfig.model_validate(model_data),
            decision_policies=DecisionPolicySet.model_validate(documents["decision_policies"][0]),
            demo=DemoProfile.model_validate(documents["demo"][0]),
            broker_shadow=DemoProfile.model_validate(documents["broker_shadow"][0]),
            live=LiveProfile.model_validate(documents["live"][0]),
            source_hashes={name: digest for name, (_, digest) in documents.items()},
        )
    except ValueError as exc:
        raise ConfigurationError(f"configuration validation failed: {exc}") from exc
