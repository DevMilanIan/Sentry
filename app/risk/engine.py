from __future__ import annotations

from datetime import timedelta
from decimal import Decimal
from enum import StrEnum

from app.clock.base import Clock
from app.config import RiskConfig
from app.domain.enums import ExecutionEnvironment, OptionType, OrderSide
from app.domain.models import AccountSnapshot, OptionQuote, RiskDecision, TradeProposal


class RiskRule(StrEnum):
    """Stable rule identifiers persisted with each risk decision."""

    ENVIRONMENT_MATCH = "environment_match"
    CONFIG_VERSION_MATCH = "risk_config_version_match"
    ACCOUNT_STATE_KNOWN = "account_state_known"
    LIVE_ACCOUNT_AUTHENTICATED = "live_account_authenticated"
    ACCOUNT_FRESH = "account_data_fresh"
    QUOTE_FRESH = "market_data_fresh"
    QUOTE_MATCH = "exact_quote_matches_proposal"
    ENTRY_SIDE = "entry_is_buy_to_open"
    ALLOWED_INSTRUMENT = "allowed_long_option"
    CONTRACT_LIMIT = "maximum_contracts_per_position"
    PREMIUM_LIMIT = "max_new_trade_premium_dollars"
    AGGREGATE_RISK_LIMIT = "max_total_open_option_risk_dollars"
    OPEN_POSITION_LIMIT = "max_open_positions"
    DAILY_ENTRY_LIMIT = "max_new_entries_per_day"
    CASH_AFFORDABLE = "cash_affordable"
    BUYING_POWER_AFFORDABLE = "buying_power_affordable"
    LIVE_CAPITAL_CEILING = "live_capital_ceiling"
    NONZERO_BID = "nonzero_bid"
    MINIMUM_OPEN_INTEREST = "minimum_open_interest"
    MINIMUM_OPTION_VOLUME = "minimum_option_volume"
    MAXIMUM_BID_ASK_SPREAD = "maximum_bid_ask_percent"


class RiskEngine:
    """Pure risk evaluator.

    The engine has no network, persistence, model, or broker authority.  Its only
    notion of time is the injected clock, which makes the same evaluation usable
    during accelerated replay.
    """

    def __init__(
        self,
        config: RiskConfig,
        clock: Clock,
        *,
        maximum_quote_age: timedelta = timedelta(seconds=120),
        maximum_account_age: timedelta = timedelta(seconds=60),
        live_capital_ceiling: Decimal | None = None,
    ) -> None:
        if maximum_quote_age.total_seconds() <= 0 or maximum_account_age.total_seconds() <= 0:
            raise ValueError("freshness windows must be positive")
        if live_capital_ceiling is not None and live_capital_ceiling <= 0:
            raise ValueError("live capital ceiling must be positive")
        self._config = config
        self._clock = clock
        self._maximum_quote_age = maximum_quote_age
        self._maximum_account_age = maximum_account_age
        self._live_capital_ceiling = live_capital_ceiling

    @property
    def config_version(self) -> str:
        return self._config.version

    def evaluate(
        self,
        proposal: TradeProposal,
        account: AccountSnapshot,
        quote: OptionQuote,
    ) -> RiskDecision:
        """Evaluate one exact order against current immutable evidence.

        V1 entries are long-option debit orders. Sell-to-close exits are
        risk-reducing and therefore bypass entry-count/capital limits, but they
        still fail closed on identity, environment, account, and quote freshness.
        """

        now = self._clock.now()
        outcomes: list[tuple[RiskRule, bool]] = []

        def check(rule: RiskRule, passed: bool) -> None:
            outcomes.append((rule, bool(passed)))

        check(
            RiskRule.ENVIRONMENT_MATCH,
            proposal.environment is account.environment,
        )
        check(RiskRule.CONFIG_VERSION_MATCH, proposal.risk_config_version == self._config.version)
        check(RiskRule.ACCOUNT_STATE_KNOWN, account.state_known)
        check(
            RiskRule.LIVE_ACCOUNT_AUTHENTICATED,
            proposal.environment is not ExecutionEnvironment.LIVE or account.is_authenticated,
        )

        account_age = now - account.as_of
        quote_age = now - quote.metadata.observed_at
        account_fresh = timedelta(0) <= account_age <= self._maximum_account_age
        quote_fresh = (
            timedelta(0) <= quote_age <= self._maximum_quote_age
            and quote.metadata.effective_at <= quote.metadata.observed_at <= now
            and proposal.quote_as_of <= now
        )
        check(RiskRule.ACCOUNT_FRESH, account_fresh)
        check(RiskRule.QUOTE_FRESH, quote_fresh)
        check(
            RiskRule.QUOTE_MATCH,
            quote.snapshot_id == proposal.quote_snapshot_id
            and quote.contract == proposal.contract
            and proposal.quote_as_of == quote.metadata.observed_at,
        )

        if proposal.side is OrderSide.SELL_TO_CLOSE:
            # An exit cannot add option risk. Broker/position reconciliation owns
            # the check that the closing quantity does not exceed the position.
            return self._decision(
                proposal,
                account,
                outcomes,
                Decimal("0"),
                account.open_option_risk,
            )

        check(RiskRule.ENTRY_SIDE, proposal.side is OrderSide.BUY_TO_OPEN)
        instrument_name = (
            "long_call" if proposal.contract.option_type is OptionType.CALL else "long_put"
        )
        check(
            RiskRule.ALLOWED_INSTRUMENT,
            instrument_name in self._config.strategy.allowed_instruments,
        )
        check(
            RiskRule.CONTRACT_LIMIT,
            proposal.quantity <= self._config.strategy.maximum_contracts_per_position,
        )

        proposed_loss = proposal.max_loss
        resulting_risk = account.open_option_risk + proposed_loss
        check(
            RiskRule.PREMIUM_LIMIT,
            proposed_loss <= self._config.risk.max_new_trade_premium_dollars,
        )
        check(
            RiskRule.AGGREGATE_RISK_LIMIT,
            resulting_risk <= self._config.risk.max_total_open_option_risk_dollars,
        )
        check(
            RiskRule.OPEN_POSITION_LIMIT,
            account.open_positions < self._config.risk.max_open_positions,
        )
        if self._config.risk.daily_new_entries_enabled:
            check(
                RiskRule.DAILY_ENTRY_LIMIT,
                account.new_entries_today < self._config.risk.max_new_entries_per_day,
            )
        else:
            check(RiskRule.DAILY_ENTRY_LIMIT, True)
        check(RiskRule.CASH_AFFORDABLE, account.cash >= proposed_loss)
        check(RiskRule.BUYING_POWER_AFFORDABLE, account.buying_power >= proposed_loss)
        check(
            RiskRule.LIVE_CAPITAL_CEILING,
            proposal.environment is not ExecutionEnvironment.LIVE
            or self._live_capital_ceiling is not None
            and resulting_risk <= self._live_capital_ceiling,
        )

        if self._config.liquidity.require_nonzero_bid:
            check(RiskRule.NONZERO_BID, quote.bid > 0)
        else:
            check(RiskRule.NONZERO_BID, True)
        check(
            RiskRule.MINIMUM_OPEN_INTEREST,
            quote.open_interest is not None
            and quote.open_interest >= self._config.liquidity.starting_minimum_open_interest,
        )
        check(
            RiskRule.MINIMUM_OPTION_VOLUME,
            quote.volume is not None
            and quote.volume >= self._config.liquidity.starting_minimum_option_volume,
        )
        spread_percent = (
            ((quote.ask - quote.bid) / quote.ask) * Decimal("100")
            if quote.ask > 0
            else Decimal("Infinity")
        )
        check(
            RiskRule.MAXIMUM_BID_ASK_SPREAD,
            spread_percent <= self._config.liquidity.starting_maximum_bid_ask_percent,
        )

        return self._decision(proposal, account, outcomes, proposed_loss, resulting_risk)

    def _decision(
        self,
        proposal: TradeProposal,
        account: AccountSnapshot,
        outcomes: list[tuple[RiskRule, bool]],
        proposed_loss: Decimal,
        resulting_risk: Decimal,
    ) -> RiskDecision:
        failed = tuple(rule.value for rule, passed in outcomes if not passed)
        passed = tuple(rule.value for rule, passed in outcomes if passed)
        return RiskDecision(
            created_at=self._clock.now(),
            environment=proposal.environment,
            proposal_id=proposal.proposal_id,
            allowed=not failed,
            failed_rules=failed,
            passed_rules=passed,
            account_snapshot_id=account.snapshot_id,
            proposed_max_loss=proposed_loss,
            resulting_aggregate_risk=resulting_risk,
            data_fresh=(
                RiskRule.ACCOUNT_FRESH.value in passed and RiskRule.QUOTE_FRESH.value in passed
            ),
            risk_config_version=self._config.version,
        )
