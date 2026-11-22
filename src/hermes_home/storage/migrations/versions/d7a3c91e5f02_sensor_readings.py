"""sensor readings: step-function history copied from HA's recorder

Two new tables. No existing table is touched.

Revision ID: d7a3c91e5f02
Revises: b4e28d1f7a55
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

from hermes_home.storage.types import UtcDateTime

revision = "d7a3c91e5f02"
down_revision = "b4e28d1f7a55"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "sensor_states",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("sensor_key", sa.String(length=64), nullable=False),
        sa.Column("measurement", sa.String(length=64), nullable=False),
        sa.Column("started_at", UtcDateTime(), nullable=False),
        sa.Column("value", sa.Float(), nullable=True),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("raw_state", sa.String(length=255), nullable=True),
        sa.Column("unit", sa.String(length=32), nullable=True),
        sa.PrimaryKeyConstraint("id", name="pk_sensor_states"),
        # The unique constraint's index also serves every range scan, which is
        # always (sensor_key, measurement) plus a started_at bound.
        sa.UniqueConstraint(
            "sensor_key", "measurement", "started_at", name="sensor_measurement_instant"
        ),
    )
    op.create_table(
        "sensor_sync_state",
        sa.Column("sensor_key", sa.String(length=64), nullable=False),
        sa.Column("measurement", sa.String(length=64), nullable=False),
        sa.Column("entity_id", sa.String(length=255), nullable=False),
        sa.Column("synced_through", UtcDateTime(), nullable=False),
        sa.Column("first_synced_at", UtcDateTime(), nullable=False),
        sa.Column("last_attempt_at", UtcDateTime(), nullable=True),
        sa.Column("last_success_at", UtcDateTime(), nullable=True),
        sa.Column("last_error", sa.String(length=255), nullable=True),
        sa.Column("unit", sa.String(length=32), nullable=True),
        sa.PrimaryKeyConstraint("sensor_key", "measurement", name="pk_sensor_sync_state"),
    )


def downgrade() -> None:
    op.drop_table("sensor_sync_state")
    op.drop_table("sensor_states")
