"""camera health: current state and historical intervals

Two new tables, no existing table touched, so every stored event stays exactly
where it was and remains queryable.

Revision ID: 9c1a4f7b2e30
Revises: 421395fd4005
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

from hermes_home.storage.types import UtcDateTime

revision = "9c1a4f7b2e30"
down_revision = "421395fd4005"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "camera_health",
        sa.Column("camera_key", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("pending_status", sa.String(length=16), nullable=True),
        sa.Column("observed_status", sa.String(length=16), nullable=False),
        sa.Column("reason", sa.String(length=64), nullable=True),
        sa.Column("camera_state", sa.String(length=64), nullable=True),
        sa.Column("image_state", sa.String(length=64), nullable=True),
        sa.Column("checked_at", UtcDateTime(), nullable=False),
        sa.Column("last_healthy_at", UtcDateTime(), nullable=True),
        sa.Column("offline_since", UtcDateTime(), nullable=True),
        sa.Column("last_image_update_at", UtcDateTime(), nullable=True),
        sa.Column("consecutive_failures", sa.Integer(), nullable=False),
        sa.Column("first_observed_at", UtcDateTime(), nullable=False),
        sa.PrimaryKeyConstraint("camera_key", name="pk_camera_health"),
    )
    op.create_table(
        "camera_health_intervals",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("camera_key", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("reason", sa.String(length=64), nullable=True),
        sa.Column("started_at", UtcDateTime(), nullable=False),
        sa.Column("ended_at", UtcDateTime(), nullable=True),
        sa.Column("observed_through", UtcDateTime(), nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_camera_health_intervals"),
    )
    op.create_index(
        "ix_camera_health_intervals_camera_started",
        "camera_health_intervals",
        ["camera_key", "started_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_camera_health_intervals_camera_started", table_name="camera_health_intervals")
    op.drop_table("camera_health_intervals")
    op.drop_table("camera_health")
