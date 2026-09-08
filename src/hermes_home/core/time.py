"""Time handling.

Every timestamp in this system is timezone-aware and stored in UTC. The rule is
enforced mechanically by :class:`hermes_home.storage.types.UtcDateTime`, which
raises on naive input; this module supplies the helpers that make obeying it easy.

Local time exists only at the presentation boundary (:func:`to_display_tz`).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo


class NaiveDatetimeError(ValueError):
    """Raised when a datetime without tzinfo reaches a boundary that requires UTC."""


def now_utc() -> datetime:
    """Current time, timezone-aware, in UTC."""
    return datetime.now(UTC)


def ensure_utc(value: datetime) -> datetime:
    """Return ``value`` as tz-aware UTC, rejecting naive datetimes.

    We refuse rather than assume, because guessing wrong here is unrecoverable:
    a table holding a mix of UTC and local timestamps carries nothing that says
    which rows need shifting.
    """
    if value.tzinfo is None or value.tzinfo.utcoffset(value) is None:
        raise NaiveDatetimeError(
            f"naive datetime {value!r} is not allowed; attach a timezone (use now_utc())"
        )
    return value.astimezone(UTC)


def parse_ha_timestamp(raw: str) -> datetime:
    """Parse a timestamp emitted by Home Assistant into tz-aware UTC.

    HA emits ISO 8601, usually with an explicit offset, sometimes with a
    trailing ``Z`` that older Pythons will not parse.
    """
    text = raw.strip()
    if text.endswith(("Z", "z")):
        text = f"{text[:-1]}+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError as exc:
        raise ValueError(f"unparseable Home Assistant timestamp: {raw!r}") from exc
    if parsed.tzinfo is None:
        # HA occasionally emits local wall-clock with no offset. Treating that as
        # UTC would silently skew every downstream window, so refuse it.
        raise NaiveDatetimeError(
            f"Home Assistant timestamp {raw!r} carries no UTC offset; cannot interpret safely"
        )
    return parsed.astimezone(UTC)


def to_display_tz(value: datetime, tz_name: str) -> datetime:
    """Convert stored UTC to a local timezone for display only. Never for storage."""
    return ensure_utc(value).astimezone(ZoneInfo(tz_name))


def within(a: datetime, b: datetime, window: timedelta) -> bool:
    """True when ``a`` and ``b`` fall within ``window`` of each other."""
    return abs(ensure_utc(a) - ensure_utc(b)) <= window
