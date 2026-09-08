"""entity_zones: composite primary key so one entity can observe several zones

A camera is located_in exactly one zone but observes several. The original
single-column primary key allowed only one row per entity, which made
"which cameras can see the driveway" unanswerable.

SQLite cannot ALTER a primary key, and Alembic's autogenerate does not detect
the change, so the table is rebuilt explicitly here.

Revision ID: 421395fd4005
Revises: 28760d50b69b
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "421395fd4005"
down_revision = "28760d50b69b"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.rename_table("entity_zones", "entity_zones_old")
    op.create_table(
        "entity_zones",
        sa.Column("entity_id", sa.String(length=255), nullable=False),
        sa.Column("zone_id", sa.Integer(), nullable=False),
        sa.Column("role", sa.String(length=32), nullable=False),
        sa.ForeignKeyConstraint(["zone_id"], ["zones.id"], name="fk_entity_zones_zone_id_zones"),
        sa.PrimaryKeyConstraint("entity_id", "zone_id", "role", name="pk_entity_zones"),
    )
    # Existing rows carried role "observes" but in fact recorded the camera's
    # own location, so they are re-labelled on the way across. The seeder
    # rewrites both roles from config at startup regardless.
    op.execute(
        "INSERT INTO entity_zones (entity_id, zone_id, role) "
        "SELECT entity_id, zone_id, 'located_in' FROM entity_zones_old"
    )
    op.drop_table("entity_zones_old")
    op.create_index("ix_entity_zones_role_zone_id", "entity_zones", ["role", "zone_id"])


def downgrade() -> None:
    op.drop_index("ix_entity_zones_role_zone_id", table_name="entity_zones")
    op.rename_table("entity_zones", "entity_zones_new")
    op.create_table(
        "entity_zones",
        sa.Column("entity_id", sa.String(length=255), nullable=False),
        sa.Column("zone_id", sa.Integer(), nullable=False),
        sa.Column("role", sa.String(length=32), nullable=False),
        sa.ForeignKeyConstraint(["zone_id"], ["zones.id"], name="fk_entity_zones_zone_id_zones"),
        sa.PrimaryKeyConstraint("entity_id", name="pk_entity_zones"),
    )
    # Collapsing back to one row per entity: keep the location relationship.
    op.execute(
        "INSERT INTO entity_zones (entity_id, zone_id, role) "
        "SELECT entity_id, zone_id, role FROM entity_zones_new "
        "WHERE role = 'located_in'"
    )
    op.drop_table("entity_zones_new")
