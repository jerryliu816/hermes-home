"""Custom SQLAlchemy column types.

:class:`UtcDateTime` is the single most load-bearing piece of this schema.

SQLite has no datetime type, and SQLAlchemy's stock ``DateTime`` will happily
round-trip a *naive* string, dropping tzinfo on the way in and handing it back
without one. If a single ingest path ever writes local time, the table ends up
holding a mix of UTC and local timestamps with nothing in the data to say which
is which -- and every temporal query and correlation window is quietly wrong,
permanently. So we refuse naive datetimes at the boundary rather than guess.
"""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import String
from sqlalchemy.types import TypeDecorator

from hermes_home.core.time import NaiveDatetimeError

# Fixed-width and lexically sortable, so ORDER BY and BETWEEN on the stored TEXT
# agree with chronological order without any parsing.
_FORMAT = "%Y-%m-%dT%H:%M:%S.%f"


class UtcDateTime(TypeDecorator[datetime]):
    """Timezone-aware UTC datetime stored as ISO 8601 TEXT ending in ``Z``."""

    impl = String(32)
    cache_ok = True

    def process_bind_param(self, value: datetime | None, dialect: object) -> str | None:
        if value is None:
            return None
        if value.tzinfo is None or value.tzinfo.utcoffset(value) is None:
            raise NaiveDatetimeError(
                f"refusing to store naive datetime {value!r}; use hermes_home.core.time.now_utc()"
            )
        return value.astimezone(UTC).strftime(_FORMAT) + "Z"

    def process_result_value(self, value: str | None, dialect: object) -> datetime | None:
        if value is None:
            return None
        return datetime.strptime(value.removesuffix("Z"), _FORMAT).replace(tzinfo=UTC)
