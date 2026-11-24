"""Exceptions for the Withings Aura server."""


class AuraError(Exception):
    """Base exception."""


class AuraConnectionError(AuraError):
    """The bridge could not be reached or the connection was lost."""


class AuraTimeoutError(AuraError):
    """The bridge did not answer in time."""


class AuraProtocolError(AuraError):
    """The bridge sent data that is not valid WPP."""


class ProbeChallengeError(AuraProtocolError):
    """The unit requires the association secret (ProbeChallenge)."""
