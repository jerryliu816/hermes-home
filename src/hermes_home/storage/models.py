"""Database schema.

The organising idea: **an event is an immutable, typed, timestamped observation.**
Everything expensive, fallible, or opinionated about it lives in a different table.

    event_deliveries   what arrived, and the durable work queue
    events             what happened            (immutable)
    event_tags         cross-type query surface
    event_analyses     what we inferred, one row per attempt
    incidents          what several events mean together
    zones / zone_edges / entity_zones           where things happen

That separation is what keeps the design generic. Adding Powerwall energy events
later is a new Pydantic payload model and a new ``event_type`` string -- no
migration, because nothing in ``events`` knows what a camera is.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import (
    JSON,
    Boolean,
    Float,
    ForeignKey,
    Index,
    Integer,
    MetaData,
    String,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from hermes_home.storage.types import UtcDateTime

# Required before the first migration. Without stable constraint names, Alembic's
# SQLite batch mode cannot drop constraints, and retrofitting names onto an
# existing database is genuinely miserable.
NAMING_CONVENTION = {
    "ix": "ix_%(table_name)s_%(column_0_N_name)s",
    "uq": "uq_%(table_name)s_%(column_0_N_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_N_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


class Base(DeclarativeBase):
    metadata = MetaData(naming_convention=NAMING_CONVENTION)


# --------------------------------------------------------------------------- #
# Space
# --------------------------------------------------------------------------- #


class Zone(Base):
    """A named place: a room, a threshold, an area of the property."""

    __tablename__ = "zones"

    id: Mapped[int] = mapped_column(primary_key=True)
    key: Mapped[str] = mapped_column(String(64), unique=True)
    name: Mapped[str] = mapped_column(String(128))
    kind: Mapped[str] = mapped_column(String(32))  # interior|exterior|threshold|structure
    parent_zone_id: Mapped[int | None] = mapped_column(ForeignKey("zones.id"))
    # Optional geometry lives here (normalized x/y, polygons) so that adding a
    # house map later requires no migration. Semantic topology works without it.
    attributes: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)


class ZoneEdge(Base):
    """A directed relationship between zones. Insert both ways for symmetric ones.

    Populated in v1 but deliberately not traversed: the zone *schema* is
    first-class now, a path-finding *engine* is not. A future correlator can use
    adjacency without a schema change.
    """

    __tablename__ = "zone_edges"

    from_zone_id: Mapped[int] = mapped_column(ForeignKey("zones.id"), primary_key=True)
    to_zone_id: Mapped[int] = mapped_column(ForeignKey("zones.id"), primary_key=True)
    # adjacent | leads_to | overlooks
    relation: Mapped[str] = mapped_column(String(32), primary_key=True)


class EntityZone(Base):
    """How a Home Assistant entity relates to a zone.

    The primary key spans all three columns because one entity holds several
    distinct relationships at once: a camera is *located_in* exactly one zone but
    *observes* several. Collapsing those into one row per entity would make
    "which cameras can see the driveway" unanswerable.

    ``located_in`` is what resolves an incoming event to its zone;
    ``observes`` is what the spatial queries read.
    """

    __tablename__ = "entity_zones"

    entity_id: Mapped[str] = mapped_column(String(255), primary_key=True)
    zone_id: Mapped[int] = mapped_column(ForeignKey("zones.id"), primary_key=True)
    role: Mapped[str] = mapped_column(String(32), primary_key=True)  # located_in|observes

    __table_args__ = (Index("ix_entity_zones_role_zone_id", "role", "zone_id"),)


# --------------------------------------------------------------------------- #
# Incidents
# --------------------------------------------------------------------------- #


class Incident(Base):
    """Several observations that appear to be one real-world occurrence.

    Derived data, and deliberately disposable: ``correlator`` records which rule
    produced it, so improving the rule means deleting and recomputing rather
    than migrating.
    """

    __tablename__ = "incidents"

    id: Mapped[int] = mapped_column(primary_key=True)
    uid: Mapped[str] = mapped_column(String(36), unique=True)
    incident_type: Mapped[str] = mapped_column(String(64))
    zone_id: Mapped[int | None] = mapped_column(ForeignKey("zones.id"))
    started_at: Mapped[datetime] = mapped_column(UtcDateTime, index=True)
    ended_at: Mapped[datetime | None] = mapped_column(UtcDateTime)
    status: Mapped[str] = mapped_column(String(16))  # open|closed
    summary: Mapped[str | None] = mapped_column(String(1024))
    correlator: Mapped[str] = mapped_column(String(64))
    correlator_version: Mapped[int] = mapped_column(Integer, default=1)
    attributes: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(UtcDateTime)
    updated_at: Mapped[datetime] = mapped_column(UtcDateTime)


# --------------------------------------------------------------------------- #
# Events
# --------------------------------------------------------------------------- #


class Event(Base):
    """One immutable observation.

    Deliberately absent, and each for a reason:

    ``importance``/``severity``  A policy judgment that changes when the rules
        change. It belongs to the analysis, not to the observation.
    ``camera_entity_id``, ``image_url``, ``person_count``  Camera-specific. The
        moment a camera column appears here, the generic design is dead;
        ``source_entity_id`` plus ``payload`` already cover it.
    ``status``/``processed``  Mutable workflow state on an immutable row. That
        lives on the delivery.
    ``updated_at``  Events do not update. The only fields ever mutated are
        ``duplicate_count`` and ``incident_id``, both by explicit operations.
    """

    __tablename__ = "events"

    id: Mapped[int] = mapped_column(primary_key=True)
    uid: Mapped[str] = mapped_column(String(36), unique=True)

    # Stage-A dedupe. The UNIQUE constraint -- not the queue -- is what makes
    # ingestion exactly-once: a redelivered or replayed job cannot insert twice.
    delivery_key: Mapped[str] = mapped_column(String(64), unique=True)

    # Dotted namespace ("camera.person_detected", later "energy.grid_outage").
    # A plain String, never a CHECK constraint or enum: constraining it would
    # mean a migration for every new event type, re-introducing exactly the
    # coupling that JSON payloads exist to avoid. Validation happens against the
    # Python registry at the write boundary instead.
    event_type: Mapped[str] = mapped_column(String(64))

    source: Mapped[str] = mapped_column(String(32))
    source_entity_id: Mapped[str | None] = mapped_column(String(255))
    zone_id: Mapped[int | None] = mapped_column(ForeignKey("zones.id"))

    occurred_at: Mapped[datetime] = mapped_column(UtcDateTime)  # in the world
    received_at: Mapped[datetime] = mapped_column(UtcDateTime)  # at our webhook
    source_state_ts: Mapped[datetime | None] = mapped_column(UtcDateTime)  # image freshness

    # Named generically, not image_hash: an energy event could hash a payload
    # snapshot. Also the join key that makes future image retention additive.
    content_hash: Mapped[str | None] = mapped_column(String(64))
    duplicate_count: Mapped[int] = mapped_column(Integer, default=0)
    incident_id: Mapped[int | None] = mapped_column(ForeignKey("incidents.id"))

    payload: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    payload_schema_version: Mapped[int] = mapped_column(Integer, default=1)
    created_at: Mapped[datetime] = mapped_column(UtcDateTime)

    __table_args__ = (
        Index("ix_events_occurred_at", "occurred_at"),
        Index("ix_events_type_occurred", "event_type", "occurred_at"),
        Index("ix_events_entity_occurred", "source_entity_id", "occurred_at"),
        Index("ix_events_zone_occurred", "zone_id", "occurred_at"),
        Index("ix_events_content_hash", "content_hash", "occurred_at"),
        Index("ix_events_incident_id", "incident_id"),
    )


class EventTag(Base):
    """Flat labels that span event types.

    The highest value-per-line in the schema: ``person_present``,
    ``package_present``, later ``grid_down`` -- one indexed filter that works
    across every future event type without any reader knowing a payload shape.
    An unknown value produces no tag, which is exactly right.
    """

    __tablename__ = "event_tags"

    event_id: Mapped[int] = mapped_column(
        ForeignKey("events.id", ondelete="CASCADE"), primary_key=True
    )
    tag: Mapped[str] = mapped_column(String(64), primary_key=True)
    source: Mapped[str] = mapped_column(String(24))  # ingest|vision|rule

    __table_args__ = (Index("ix_event_tags_tag_event_id", "tag", "event_id"),)


class EventAnalysis(Base):
    """One vision attempt against one event.

    A separate table rather than columns on ``events`` because a retry should
    add a row, not destroy the failure that preceded it -- keeping the failure
    record is the entire point of "mark analysis as failed". Events that are
    never analyzed (energy, sensor) simply have no rows here.

    The dimension/token/latency fields exist because the image is discarded:
    they are the only forensic trail left, cheap to store and impossible to
    reconstruct later.
    """

    __tablename__ = "event_analyses"

    id: Mapped[int] = mapped_column(primary_key=True)
    event_id: Mapped[int] = mapped_column(ForeignKey("events.id", ondelete="CASCADE"))
    attempt: Mapped[int] = mapped_column(Integer, default=1)
    kind: Mapped[str] = mapped_column(String(32), default="vision_scene")
    status: Mapped[str] = mapped_column(String(16))  # ok|failed|refused|skipped

    provider: Mapped[str] = mapped_column(String(64))
    model: Mapped[str | None] = mapped_column(String(128))
    prompt_version: Mapped[str] = mapped_column(String(32))

    observation: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    observation_schema_version: Mapped[int] = mapped_column(Integer, default=1)

    error_code: Mapped[str | None] = mapped_column(String(64))
    error_message: Mapped[str | None] = mapped_column(String(1024))
    retryable: Mapped[bool | None] = mapped_column(Boolean)

    started_at: Mapped[datetime] = mapped_column(UtcDateTime)
    completed_at: Mapped[datetime | None] = mapped_column(UtcDateTime)
    latency_ms: Mapped[int | None] = mapped_column(Integer)

    artifact_bytes: Mapped[int | None] = mapped_column(Integer)
    artifact_width: Mapped[int | None] = mapped_column(Integer)
    artifact_height: Mapped[int | None] = mapped_column(Integer)
    input_tokens: Mapped[int | None] = mapped_column(Integer)
    output_tokens: Mapped[int | None] = mapped_column(Integer)
    cost_usd: Mapped[float | None] = mapped_column(Float)

    __table_args__ = (
        UniqueConstraint("event_id", "attempt", "kind", name="event_attempt_kind"),
        Index("ix_event_analyses_event_id_status", "event_id", "status"),
    )


# --------------------------------------------------------------------------- #
# Deliveries: the audit log and the durable work queue, in one table
# --------------------------------------------------------------------------- #


class DeliveryStatus:
    PENDING = "pending"
    PROCESSING = "processing"
    COMPLETED = "completed"
    FAILED = "failed"
    REJECTED = "rejected"


class Disposition:
    ACCEPTED = "accepted"
    DUPLICATE_DELIVERY = "duplicate_delivery"
    DUPLICATE_CONTENT = "duplicate_content"
    REJECTED_INVALID = "rejected_invalid"
    REJECTED_STALE_IMAGE = "rejected_stale_image"


class EventDelivery(Base):
    """Every inbound webhook, and the unit of background work it becomes.

    One table rather than two, because a delivery and a job are the same thing,
    one-to-one, forever; a parallel ``ingest_jobs`` table would be symmetry for
    its own sake. ``events`` stays immutable, and this -- already the mutable,
    auditable side of the system -- carries the lifecycle.

    A suppressed duplicate is recorded *here*, pointing at the canonical event,
    and never as a row in ``events``. Dropping duplicates silently makes dedupe
    bugs invisible exactly when they matter; flagging them inside ``events``
    would poison every query with a ``WHERE NOT is_duplicate`` someone forgets.
    """

    __tablename__ = "event_deliveries"

    id: Mapped[int] = mapped_column(primary_key=True)
    uid: Mapped[str] = mapped_column(String(36), unique=True)
    received_at: Mapped[datetime] = mapped_column(UtcDateTime, index=True)
    source: Mapped[str] = mapped_column(String(32))
    delivery_key: Mapped[str] = mapped_column(String(64), index=True)
    correlation_id: Mapped[str] = mapped_column(String(36))

    # Queue lifecycle
    status: Mapped[str] = mapped_column(String(16), default=DeliveryStatus.PENDING)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    next_attempt_at: Mapped[datetime] = mapped_column(UtcDateTime)
    lease_expires_at: Mapped[datetime | None] = mapped_column(UtcDateTime)
    last_error_code: Mapped[str | None] = mapped_column(String(64))
    last_error_message: Mapped[str | None] = mapped_column(String(1024))

    # Terminal outcome, once known
    disposition: Mapped[str | None] = mapped_column(String(24))
    event_id: Mapped[int | None] = mapped_column(ForeignKey("events.id"))
    note: Mapped[str | None] = mapped_column(String(512))

    # Bounded retention: nulled by the pruner after DELIVERY_RAW_RETENTION_DAYS.
    # Everything above it is metadata and is kept permanently. Headers are never
    # stored -- they carry the shared secret.
    raw_body: Mapped[dict[str, Any] | None] = mapped_column(JSON)

    __table_args__ = (
        Index("ix_event_deliveries_status_next_attempt_at", "status", "next_attempt_at"),
        Index("ix_event_deliveries_disposition", "disposition"),
    )
