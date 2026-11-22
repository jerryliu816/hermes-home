"""Arithmetic over a measurement's step function. Pure: no database, no clock.

A stored measurement is a list of steps -- each value held from its start until
the next step. Every question asked of it (lowest overnight, how long out of
range, the hourly trend) is a question about time-weighted spans, not about
samples: a value that held for six hours must count six hours, and a report
that lasted a second must not count as much as one that lasted all night.

Unknown is carried through as ``value=None``. It is excluded from every
statistic and reported as its own span, so a sensor that went silent reads as
"not known from 02:10 to 04:40", never as a flat line through the gap and never
as zero.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime, time, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from hermes_home.config import MeasurementConfig

#: Reasons a span is unknown that come from reading, not from Home Assistant.
BEFORE_RECORD = "before_record"
NOT_YET_SYNCED = "not_yet_synced"
UNIT_MISMATCH = "unit_mismatch"

#: Bucket widths a trend may use, smallest first. Round numbers so "the 2 AM
#: bucket" means something to a reader.
_BUCKET_WIDTHS = [
    timedelta(minutes=m) for m in (5, 10, 15, 30, 60, 120, 180, 360, 720, 1440, 2880, 10080)
]


@dataclass(frozen=True)
class Step:
    started_at: datetime
    value: float | None
    status: str


@dataclass(frozen=True)
class Segment:
    start: datetime
    end: datetime
    value: float | None
    status: str

    @property
    def seconds(self) -> float:
        return (self.end - self.start).total_seconds()

    @property
    def known(self) -> bool:
        return self.value is not None


# --------------------------------------------------------------------------- #
# Units
# --------------------------------------------------------------------------- #

_TEMPERATURE = {"°c": "c", "c": "c", "°f": "f", "f": "f", "k": "k"}


def convert(value: float, from_unit: str | None, to_unit: str | None) -> float | None:
    """``value`` in ``to_unit``, or None when the units cannot be reconciled.

    Only temperature is converted. Any other mismatch is not guessed at: a
    reading whose unit is not the one being reported becomes unknown.
    """
    if to_unit is None or from_unit == to_unit:
        return value
    if from_unit is None:
        return None
    src = _TEMPERATURE.get(from_unit.strip().lower())
    dst = _TEMPERATURE.get(to_unit.strip().lower())
    if src is None or dst is None:
        return None
    celsius = {"c": value, "f": (value - 32) * 5 / 9, "k": value - 273.15}[src]
    return {"c": celsius, "f": celsius * 9 / 5 + 32, "k": celsius + 273.15}[dst]


# --------------------------------------------------------------------------- #
# Building segments
# --------------------------------------------------------------------------- #


def segments(
    steps: list[Step],
    *,
    start: datetime,
    end: datetime,
    known_through: datetime | None,
) -> list[Segment]:
    """The window [start, end) as contiguous segments, every second accounted for.

    ``steps`` must be oldest first and may begin before ``start`` (the step in
    force at ``start``). Time before the first step is ``before_record``; time
    after ``known_through`` -- the point synced from Home Assistant -- is
    ``not_yet_synced``. Neither is ever filled in by extending a value.
    """
    if end <= start:
        return []
    out: list[Segment] = []
    first = steps[0].started_at if steps else end
    if first > start:
        out.append(Segment(start, min(first, end), None, BEFORE_RECORD))
    for i, step in enumerate(steps):
        seg_start = max(step.started_at, start)
        seg_end = min(steps[i + 1].started_at if i + 1 < len(steps) else end, end)
        if seg_end > seg_start:
            out.append(Segment(seg_start, seg_end, step.value, step.status))

    horizon = known_through if known_through is not None else start
    if horizon >= end:
        return out
    cut: list[Segment] = []
    for seg in out:
        if seg.end <= horizon:
            cut.append(seg)
        elif seg.start < horizon:
            cut.append(Segment(seg.start, horizon, seg.value, seg.status))
    cut.append(Segment(max(horizon, start), end, None, NOT_YET_SYNCED))
    return cut


# --------------------------------------------------------------------------- #
# Ranges
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class Limits:
    min: float | None
    max: float | None
    period: str  # "day" | "night" | "always"

    def verdict(self, value: float) -> str | None:
        """'below', 'above', or None when within limits."""
        if self.min is not None and value < self.min:
            return "below"
        if self.max is not None and value > self.max:
            return "above"
        return None


def _clock(text: str) -> time:
    hours, minutes = text.split(":")
    return time(int(hours), int(minutes))


def _is_night(local: datetime, start: time, stop: time) -> bool:
    moment = local.time()
    if start < stop:
        return start <= moment < stop
    return moment >= start or moment < stop  # crosses midnight


def limits_at(cfg: MeasurementConfig, moment: datetime, tz: str) -> Limits | None:
    """The limits in force at ``moment``, or None when no range is configured."""
    if cfg.range is None:
        return None
    if cfg.night is not None:
        local = moment.astimezone(ZoneInfo(tz))
        if _is_night(local, _clock(cfg.night.from_time), _clock(cfg.night.to_time)):
            return Limits(cfg.night.min, cfg.night.max, "night")
        return Limits(cfg.range.min, cfg.range.max, "day")
    return Limits(cfg.range.min, cfg.range.max, "always")


def _night_boundaries(cfg: MeasurementConfig, start: datetime, end: datetime, tz: str) -> list:
    """Every instant in (start, end) where the night range begins or ends."""
    if cfg.night is None:
        return []
    zone = ZoneInfo(tz)
    first = start.astimezone(zone).date() - timedelta(days=1)
    last = end.astimezone(zone).date() + timedelta(days=1)
    marks: list[datetime] = []
    day = first
    while day <= last:
        for clock in (_clock(cfg.night.from_time), _clock(cfg.night.to_time)):
            instant = datetime.combine(day, clock, tzinfo=zone).astimezone(start.tzinfo)
            if start < instant < end:
                marks.append(instant)
        day += timedelta(days=1)
    return sorted(marks)


def split_at(segs: list[Segment], marks: list[datetime]) -> list[Segment]:
    """Cut segments at each mark, so no piece straddles a change of limits."""
    if not marks:
        return segs
    out: list[Segment] = []
    for seg in segs:
        cursor = seg.start
        for mark in marks:
            if cursor < mark < seg.end:
                out.append(Segment(cursor, mark, seg.value, seg.status))
                cursor = mark
        out.append(Segment(cursor, seg.end, seg.value, seg.status))
    return out


def out_of_range(
    segs: list[Segment], cfg: MeasurementConfig, tz: str
) -> tuple[list[dict[str, Any]], float]:
    """Contiguous spans outside the configured limits, and their total seconds.

    A span is broken by unknown time: what happened during a gap is not known,
    so a cold spell either side of one is two spans, not one long one.
    """
    if cfg.range is None or not segs:
        return [], 0.0
    pieces = split_at(segs, _night_boundaries(cfg, segs[0].start, segs[-1].end, tz))
    spans: list[dict[str, Any]] = []
    total = 0.0
    current: dict[str, Any] | None = None
    for piece in pieces:
        verdict = None
        limits = None
        if piece.known:
            limits = limits_at(cfg, piece.start, tz)
            verdict = limits.verdict(piece.value) if limits else None
        if verdict is None:
            current = None
            continue
        total += piece.seconds
        contiguous = current is not None and current["_end"] == piece.start
        if contiguous and current["direction"] == verdict:
            current["_end"] = piece.end
            if (piece.value < current["extreme"]) == (verdict == "below"):
                current["extreme"], current["_extreme_at"] = piece.value, piece.start
                current["limit"] = limits.min if verdict == "below" else limits.max
        else:
            current = {
                "direction": verdict,
                "_start": piece.start,
                "_end": piece.end,
                "extreme": piece.value,
                "_extreme_at": piece.start,
                "limit": limits.min if verdict == "below" else limits.max,
            }
            spans.append(current)
    return spans, total


# --------------------------------------------------------------------------- #
# Statistics
# --------------------------------------------------------------------------- #


def summarize(segs: list[Segment]) -> dict[str, Any]:
    """Time-weighted mean, extremes with when they began, and how much was known."""
    known = [s for s in segs if s.known and s.seconds > 0]
    total = sum(s.seconds for s in segs)
    known_seconds = sum(s.seconds for s in known)
    summary: dict[str, Any] = {
        "known_seconds": round(known_seconds),
        "unknown_seconds": round(total - known_seconds),
        "known_fraction": round(known_seconds / total, 3) if total else None,
        "min": None,
        "min_at": None,
        "max": None,
        "max_at": None,
        "mean": None,
    }
    if not known:
        return summary
    low = min(known, key=lambda s: s.value)
    high = max(known, key=lambda s: s.value)
    summary.update(
        min=low.value,
        min_at=low.start,
        max=high.value,
        max_at=high.start,
        mean=sum(s.value * s.seconds for s in known) / known_seconds,
    )
    return summary


def unknown_spans(segs: list[Segment]) -> list[dict[str, Any]]:
    """Unknown time, merged where the reason does not change."""
    spans: list[dict[str, Any]] = []
    for seg in segs:
        if seg.known:
            continue
        last = spans[-1] if spans else None
        if last is not None and last["reason"] == seg.status and last["_end"] == seg.start:
            last["_end"] = seg.end
        else:
            spans.append({"reason": seg.status, "_start": seg.start, "_end": seg.end})
    return spans


def bucket_width(start: datetime, end: datetime, max_buckets: int) -> timedelta:
    """The smallest round width that covers the window in at most ``max_buckets``."""
    span = (end - start).total_seconds()
    for width in _BUCKET_WIDTHS:
        if math.ceil(span / width.total_seconds()) <= max_buckets:
            return width
    return _BUCKET_WIDTHS[-1]


def buckets(
    segs: list[Segment], *, start: datetime, end: datetime, max_buckets: int
) -> tuple[timedelta, list[dict[str, Any]]]:
    """min / mean / max per bucket, each with how much of it was known."""
    width = bucket_width(start, end, max_buckets)
    out: list[dict[str, Any]] = []
    cursor = start
    while cursor < end:
        stop = min(cursor + width, end)
        inside = [
            Segment(max(s.start, cursor), min(s.end, stop), s.value, s.status)
            for s in segs
            if s.end > cursor and s.start < stop
        ]
        stats = summarize(inside)
        out.append(
            {
                "_start": cursor,
                "min": stats["min"],
                "mean": stats["mean"],
                "max": stats["max"],
                "known_fraction": stats["known_fraction"],
            }
        )
        cursor = stop
    return width, out
