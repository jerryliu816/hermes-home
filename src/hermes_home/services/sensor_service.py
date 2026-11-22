"""Read side for sensor readings, shared by the MCP tools and the CLI.

Results are plain JSON-ready dicts. Times are given in the display timezone
with their offset, which is unambiguous and is what a reader reasons in.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from hermes_home.config import MeasurementConfig, SensorConfig, SensorsConfig, Settings
from hermes_home.core.time import ensure_utc, now_utc, to_display_tz
from hermes_home.sensors import series
from hermes_home.sensors.series import Step
from hermes_home.storage.models import SensorState, SensorStatus
from hermes_home.storage.repositories import SensorRepository

#: At most this many spans of each kind per series, so one flapping sensor
#: cannot blow up a result. The totals always cover all of them.
MAX_SPANS = 20


def _r(value: float | None) -> float | None:
    return None if value is None else round(value, 2)


class SensorService:
    def __init__(
        self, session: AsyncSession, *, settings: Settings, sensors: SensorsConfig
    ) -> None:
        self._settings = settings
        self._sensors = sensors
        self._repo = SensorRepository(session)
        self._tz = settings.display_timezone

    def _local(self, value: datetime | None) -> str | None:
        return to_display_tz(value, self._tz).isoformat() if value is not None else None

    # ----------------------------------------------------------------- #
    # Resolution
    # ----------------------------------------------------------------- #

    def resolve(
        self, *, sensor: str | None = None, zone: str | None = None
    ) -> tuple[list[str], str | None]:
        """Sensor keys matching a key/name/alias or a zone, plus an error if none do."""
        if sensor is not None:
            wanted = sensor.strip().casefold()
            for key, cfg in self._sensors.sensors.items():
                names = {key, cfg.name, *cfg.aliases}
                if wanted in {n.casefold() for n in names}:
                    return [key], None
            return [], (
                f"no sensor called {sensor!r}; configured sensors: "
                + (", ".join(sorted(self._sensors.sensors)) or "none")
            )
        keys = sorted(self._sensors.sensors)
        if zone is not None:
            keys = [k for k in keys if self._sensors.sensors[k].location == zone]
            if not keys:
                placed = sorted({s.location for s in self._sensors.sensors.values()})
                return [], (
                    f"no sensor is located in zone {zone!r}; zones with sensors: "
                    + (", ".join(placed) or "none")
                )
        return keys, None

    def _unit(self, cfg: MeasurementConfig, sync_unit: str | None) -> str | None:
        return cfg.unit or sync_unit

    def _steps(self, rows: list[SensorState], unit: str | None) -> list[Step]:
        steps: list[Step] = []
        for row in rows:
            value = row.value
            status = row.status
            if value is not None:
                value = series.convert(value, row.unit, unit)
                if value is None:
                    status = series.UNIT_MISMATCH
            steps.append(Step(row.started_at, value, status))
        return steps

    def _range(self, cfg: MeasurementConfig) -> dict[str, Any] | None:
        if cfg.range is None:
            return None
        out: dict[str, Any] = {"min": cfg.range.min, "max": cfg.range.max}
        if cfg.night is not None:
            out["night"] = {
                "from": cfg.night.from_time,
                "to": cfg.night.to_time,
                "min": cfg.night.min,
                "max": cfg.night.max,
            }
        return out

    # ----------------------------------------------------------------- #
    # Current readings
    # ----------------------------------------------------------------- #

    async def current(self, keys: list[str]) -> list[dict[str, Any]]:
        now = now_utc()
        out: list[dict[str, Any]] = []
        for key in keys:
            cfg = self._sensors.sensors[key]
            measurements = {
                name: await self._current_measurement(key, name, m, now)
                for name, m in sorted(cfg.measurements.items())
            }
            out.append(self._header(key, cfg) | {"measurements": measurements})
        return out

    def _header(self, key: str, cfg: SensorConfig) -> dict[str, Any]:
        return {"key": key, "name": cfg.name, "aliases": list(cfg.aliases), "zone": cfg.location}

    async def _current_measurement(
        self, key: str, name: str, cfg: MeasurementConfig, now: datetime
    ) -> dict[str, Any]:
        sync = await self._repo.sync_state(key, name)
        last = await self._repo.last_state(key, name)
        unit = self._unit(cfg, sync.unit if sync else None)
        value = status = None
        if last is not None:
            [step] = self._steps([last], unit)
            value, status = step.value, step.status
        synced_through = sync.synced_through if sync else None
        stale = synced_through is None or (
            (now - synced_through).total_seconds() > self._settings.sensor_stale_after_seconds
        )
        if stale:
            # The stored value may well still be right, but nothing confirms
            # it: say so rather than presenting it as the current reading.
            status = "stale"
        limits = series.limits_at(cfg, now, self._tz)
        in_range = None
        if limits is not None and value is not None and not stale and status == SensorStatus.OK:
            in_range = limits.verdict(value) is None
        return {
            "value": _r(value),
            "unit": unit,
            "status": status or "never_synced",
            "value_since": self._local(last.started_at) if last else None,
            "confirmed_through": self._local(synced_through),
            "range": self._range(cfg),
            "limits_now": (
                {"min": limits.min, "max": limits.max, "period": limits.period} if limits else None
            ),
            "in_range": in_range,
            "sync_error": sync.last_error if sync else None,
        }

    # ----------------------------------------------------------------- #
    # History
    # ----------------------------------------------------------------- #

    async def history(
        self,
        keys: list[str],
        measurement: str,
        *,
        start: datetime,
        end: datetime,
        max_buckets: int,
    ) -> list[dict[str, Any]]:
        start, end = ensure_utc(start), ensure_utc(end)
        out: list[dict[str, Any]] = []
        for key in keys:
            cfg = self._sensors.sensors[key]
            m = cfg.measurements.get(measurement)
            if m is None:
                out.append(
                    self._header(key, cfg)
                    | {
                        "measurement": measurement,
                        "error": f"{key!r} does not measure {measurement!r}; it measures "
                        + ", ".join(sorted(cfg.measurements)),
                    }
                )
                continue
            out.append(
                self._header(key, cfg)
                | await self._series(key, measurement, m, start, end, max_buckets)
            )
        return out

    async def _series(
        self,
        key: str,
        name: str,
        cfg: MeasurementConfig,
        start: datetime,
        end: datetime,
        max_buckets: int,
    ) -> dict[str, Any]:
        sync = await self._repo.sync_state(key, name)
        unit = self._unit(cfg, sync.unit if sync else None)
        rows = await self._repo.states_between(key, name, start=start, end=end)
        segs = series.segments(
            self._steps(rows, unit),
            start=start,
            end=end,
            known_through=sync.synced_through if sync else None,
        )

        stats = series.summarize(segs)
        gaps = series.unknown_spans(segs)
        spans, out_seconds = series.out_of_range(segs, cfg, self._tz)
        width, trend = series.buckets(segs, start=start, end=end, max_buckets=max_buckets)
        record_begins = await self._repo.first_known_at(key, name)

        def span(item: dict[str, Any], **extra: Any) -> dict[str, Any]:
            return {
                "start": self._local(item["_start"]),
                "end": self._local(item["_end"]),
                "minutes": round((item["_end"] - item["_start"]).total_seconds() / 60, 1),
                **extra,
            }

        result: dict[str, Any] = {
            "measurement": name,
            "unit": unit,
            "record_begins_at": self._local(record_begins),
            "summary": {
                "min": _r(stats["min"]),
                "min_at": self._local(stats["min_at"]),
                "max": _r(stats["max"]),
                "max_at": self._local(stats["max_at"]),
                "mean": _r(stats["mean"]),
                "known_fraction": stats["known_fraction"],
            },
            "range": self._range(cfg),
            "out_of_range": None,
            "unknown": {
                "minutes": round(stats["unknown_seconds"] / 60, 1),
                "spans": [span(g, reason=g["reason"]) for g in gaps[:MAX_SPANS]],
            },
            "trend": {
                "bucket_minutes": int(width.total_seconds() // 60),
                "buckets": [
                    {
                        "start": self._local(b["_start"]),
                        "min": _r(b["min"]),
                        "mean": _r(b["mean"]),
                        "max": _r(b["max"]),
                        **(
                            {"known_fraction": b["known_fraction"]}
                            if b["known_fraction"] != 1.0
                            else {}
                        ),
                    }
                    for b in trend
                ],
            },
        }
        if cfg.range is not None:
            result["out_of_range"] = {
                "minutes": round(out_seconds / 60, 1),
                "spans": [
                    span(
                        s,
                        direction=s["direction"],
                        extreme=_r(s["extreme"]),
                        extreme_at=self._local(s["_extreme_at"]),
                        limit=s["limit"],
                    )
                    for s in spans[:MAX_SPANS]
                ],
            }
            if len(spans) > MAX_SPANS:
                result["out_of_range"]["spans_omitted"] = len(spans) - MAX_SPANS
        if len(gaps) > MAX_SPANS:
            result["unknown"]["spans_omitted"] = len(gaps) - MAX_SPANS
        return result
