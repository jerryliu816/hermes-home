"""Identifier and content-key derivation.

Two keys matter, and they answer different questions:

``delivery_key``  Did we already receive *this exact delivery*?  Deterministic,
                  derived only from what Home Assistant told us, and backed by a
                  UNIQUE constraint so a redelivery cannot create a second event.

``content_hash``  Is this the same *picture* we just analyzed?  Derived from the
                  fetched bytes, matched inside a short time window.
"""

from __future__ import annotations

import hashlib
import uuid
from datetime import datetime

from hermes_home.core.time import ensure_utc

# Versioned so a future key definition can coexist with keys already in the
# database instead of forcing a recompute of history.
DELIVERY_KEY_VERSION = "v1"


def new_uid() -> str:
    """A public identifier. This is the only id we ever expose over MCP."""
    return str(uuid.uuid4())


def delivery_key(
    *,
    source: str,
    event_type: str,
    source_entity_id: str | None,
    occurred_at: datetime,
) -> str:
    """Deterministic identity for one inbound delivery.

    Millisecond precision: coarser (whole seconds) would permanently merge two
    genuinely distinct rapid events, finer would never match a redelivery.
    Every input is also stored as its own column on ``events`` so the key stays
    recomputable if the definition ever changes.
    """
    moment = ensure_utc(occurred_at)
    stamp = moment.strftime("%Y-%m-%dT%H:%M:%S.") + f"{moment.microsecond // 1000:03d}Z"
    material = "|".join([DELIVERY_KEY_VERSION, source, event_type, source_entity_id or "", stamp])
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


def content_hash(data: bytes) -> str:
    """SHA-256 of an image.

    Exact, not perceptual: a perceptual hash introduces a similarity threshold to
    tune, and starts merging frames that are genuinely different. Near-duplicates
    are *related*, not duplicate, and belong to incident correlation instead.
    """
    return hashlib.sha256(data).hexdigest()
