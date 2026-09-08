"""Error types shared across layers."""

from __future__ import annotations


class HermesHomeError(Exception):
    """Base class for every error this service raises deliberately."""


class ConfigError(HermesHomeError):
    """Configuration is missing or self-inconsistent. Raised at startup."""


class HomeAssistantError(HermesHomeError):
    """A Home Assistant request failed.

    ``code`` is the structured, loggable reason; it is what lands in
    ``event_deliveries.last_error_code``.
    """

    def __init__(self, code: str, message: str, *, retryable: bool = False) -> None:
        super().__init__(message)
        self.code = code
        self.retryable = retryable


class ImageUnavailableError(HomeAssistantError):
    """The camera's event image could not be retrieved, or was never fresh."""
