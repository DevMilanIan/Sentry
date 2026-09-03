from enum import IntEnum, StrEnum


class ExecutionEnvironment(StrEnum):
    DEMO = "DEMO"
    LIVE = "LIVE"


class DemoBackend(StrEnum):
    OFFLINE_SIM = "OFFLINE_SIM"
    BROKER_SHADOW = "BROKER_SHADOW"


class TradingMode(StrEnum):
    RESEARCH = "RESEARCH"
    SHADOW = "SHADOW"
    APPROVAL = "APPROVAL"
    EXIT_AUTO = "EXIT_AUTO"
    AUTO = "AUTO"


class RuntimeSafetyState(StrEnum):
    NORMAL = "NORMAL"
    ENTRY_DISABLED = "ENTRY_DISABLED"
    EXIT_ONLY = "EXIT_ONLY"
    HALTED = "HALTED"


class AttentionLevel(IntEnum):
    BACKGROUND = 0
    WATCH = 1
    CANDIDATE = 2
    TRADE_WORTHY = 3
    DEEP_RESEARCH = 4
    POSITION = 5


class Direction(StrEnum):
    BULLISH = "bullish"
    BEARISH = "bearish"
    MIXED = "mixed"
    NONE = "none"


class JudgeDecision(StrEnum):
    PASS = "PASS"  # noqa: S105 -- decision label, not a password
    WATCH = "WATCH"
    REJECT = "REJECT"


class SelectorStatus(StrEnum):
    NO_CONTRACT = "NO_CONTRACT"
    CONTRACT_FOUND = "CONTRACT_FOUND"


class OptionType(StrEnum):
    CALL = "call"
    PUT = "put"


class OrderSide(StrEnum):
    BUY_TO_OPEN = "buy_to_open"
    SELL_TO_CLOSE = "sell_to_close"


class BrokerAction(StrEnum):
    PLACE_OPTION_ORDER = "place_option_order"
    CANCEL_OPTION_ORDER = "cancel_option_order"
    REPLACE_OPTION_ORDER = "replace_option_order"


class OrderState(StrEnum):
    PROPOSED = "PROPOSED"
    REVIEWED = "REVIEWED"
    APPROVED = "APPROVED"
    INTENT_PERSISTED = "INTENT_PERSISTED"
    SUBMITTING = "SUBMITTING"
    SUBMISSION_UNKNOWN = "SUBMISSION_UNKNOWN"
    OPEN = "OPEN"
    PARTIAL = "PARTIAL"
    FILLED = "FILLED"
    CANCELED = "CANCELED"
    REJECTED = "REJECTED"
    EXPIRED = "EXPIRED"


class AccountKind(StrEnum):
    BROKER_OBSERVED = "BROKER_OBSERVED"
    SIMULATED = "SIMULATED"
    SHADOW = "SHADOW"


class FirewallDisposition(StrEnum):
    BLOCKED_SHADOW = "BLOCKED_SHADOW"
    BLOCKED_POLICY = "BLOCKED_POLICY"
    AUTHORIZED_LIVE = "AUTHORIZED_LIVE"
