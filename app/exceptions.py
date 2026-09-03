"""Application exception taxonomy used for deterministic recovery decisions."""


class SentinelError(Exception):
    """Base exception for expected application failures."""


class TransientError(SentinelError):
    """A bounded retry may succeed without changing safety assumptions."""


class DataInvalidError(SentinelError):
    """External or model data failed validation."""


class AuthenticationRequiredError(SentinelError):
    """User-owned authentication is absent or expired."""


class SafetyCriticalError(SentinelError):
    """An invariant failed; order-affecting behavior must fail closed."""


class SubmissionUnknownError(SafetyCriticalError):
    """A broker may have received a write that cannot yet be reconciled."""


class ConfigurationError(SafetyCriticalError):
    """Configuration is missing, contradictory, or more permissive than declared."""
