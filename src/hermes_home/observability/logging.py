"""Structured logging.

Every delivery carries a ``correlation_id`` from the moment the webhook accepts
it, through claiming, image retrieval, vision, and persistence -- so one grep
reconstructs the whole path of a single doorbell press.

Secrets are never passed to a log call anywhere in this codebase. The processor
below is a second line of defence, not the primary one.
"""

from __future__ import annotations

import logging
import sys
from typing import Any

import structlog

_REDACT_KEYS = {
    "token",
    "home_assistant_token",
    "webhook_secret",
    "anthropic_api_key",
    "api_key",
    "authorization",
    "secret",
    "password",
}


def _redact(_logger: object, _name: str, event_dict: dict[str, Any]) -> dict[str, Any]:
    for key in list(event_dict):
        if key.lower() in _REDACT_KEYS:
            event_dict[key] = "<redacted>"
    return event_dict


def configure_logging(*, level: str = "INFO", fmt: str = "console") -> None:
    logging.basicConfig(format="%(message)s", stream=sys.stdout, level=level.upper())

    renderer: Any = (
        structlog.dev.ConsoleRenderer() if fmt == "console" else structlog.processors.JSONRenderer()
    )
    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso", utc=True),
            _redact,
            structlog.processors.StackInfoRenderer(),
            structlog.processors.format_exc_info,
            renderer,
        ],
        wrapper_class=structlog.make_filtering_bound_logger(
            logging.getLevelNamesMapping()[level.upper()]
        ),
        cache_logger_on_first_use=True,
    )
