"""thermostat changes: an audit row for every request to set a thermostat

One new table. No existing table is touched.

Revision ID: e5b81c4a9d17
Revises: d7a3c91e5f02
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

from hermes_home.storage.types import UtcDateTime

revision = "e5b81c4a9d17"
down_revision = "d7a3c91e5f02"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "thermostat_changes",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("uid", sa.String(length=36), nullable=False),
        sa.Column("sensor_key", sa.String(length=64), nullable=False),
        sa.Column("entity_id", sa.String(length=255), nullable=False),
        sa.Column("requested_at", UtcDateTime(), nullable=False),
        sa.Column("request", sa.JSON(), nullable=False),
        sa.Column("outcome", sa.String(length=16), nullable=False),
        sa.Column("error_code", sa.String(length=64), nullable=True),
        sa.Column("detail", sa.String(length=500), nullable=True),
        sa.Column("before", sa.JSON(), nullable=True),
        sa.Column("after", sa.JSON(), nullable=True),
        sa.PrimaryKeyConstraint("id", name="pk_thermostat_changes"),
        sa.UniqueConstraint("uid", name="uq_thermostat_changes_uid"),
    )
    op.create_index("ix_thermostat_changes_sensor_key", "thermostat_changes", ["sensor_key"])
    op.create_index("ix_thermostat_changes_requested_at", "thermostat_changes", ["requested_at"])


def downgrade() -> None:
    op.drop_index("ix_thermostat_changes_requested_at", table_name="thermostat_changes")
    op.drop_index("ix_thermostat_changes_sensor_key", table_name="thermostat_changes")
    op.drop_table("thermostat_changes")
