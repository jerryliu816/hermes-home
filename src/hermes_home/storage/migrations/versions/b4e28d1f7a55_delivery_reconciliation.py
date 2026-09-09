"""delivery reconciliation: gaps between HA triggers and our deliveries

Two new tables. No existing table is touched.

Revision ID: b4e28d1f7a55
Revises: 9c1a4f7b2e30
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

from hermes_home.storage.types import UtcDateTime

revision = "b4e28d1f7a55"
down_revision = "9c1a4f7b2e30"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "camera_delivery_gaps",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("camera_key", sa.String(length=64), nullable=False),
        sa.Column("trigger_entity", sa.String(length=255), nullable=False),
        sa.Column("ha_trigger_at", UtcDateTime(), nullable=False),
        sa.Column("ha_image_ts", UtcDateTime(), nullable=True),
        sa.Column("detected_at", UtcDateTime(), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("reason", sa.String(length=64), nullable=False),
        sa.Column("matched_delivery_id", sa.Integer(), nullable=True),
        sa.ForeignKeyConstraint(
            ["matched_delivery_id"],
            ["event_deliveries.id"],
            name="fk_camera_delivery_gaps_matched_delivery_id_event_deliveries",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_camera_delivery_gaps"),
        sa.UniqueConstraint("camera_key", "ha_trigger_at", name="camera_trigger_instant"),
    )
    op.create_index(
        "ix_camera_delivery_gaps_camera_trigger",
        "camera_delivery_gaps",
        ["camera_key", "ha_trigger_at"],
    )
    op.create_index("ix_camera_delivery_gaps_status", "camera_delivery_gaps", ["status"])

    op.create_table(
        "camera_pipeline_state",
        sa.Column("camera_key", sa.String(length=64), nullable=False),
        sa.Column("last_checked_at", UtcDateTime(), nullable=False),
        sa.Column("checked_through", UtcDateTime(), nullable=False),
        sa.Column("first_checked_at", UtcDateTime(), nullable=False),
        sa.Column("last_verified_delivery_at", UtcDateTime(), nullable=True),
        sa.PrimaryKeyConstraint("camera_key", name="pk_camera_pipeline_state"),
    )


def downgrade() -> None:
    op.drop_table("camera_pipeline_state")
    op.drop_index("ix_camera_delivery_gaps_status", table_name="camera_delivery_gaps")
    op.drop_index("ix_camera_delivery_gaps_camera_trigger", table_name="camera_delivery_gaps")
    op.drop_table("camera_delivery_gaps")
