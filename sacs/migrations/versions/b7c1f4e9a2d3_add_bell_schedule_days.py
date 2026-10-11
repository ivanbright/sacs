"""add bell schedule days

Adds an optional ``days`` JSON column to ``bell_schedules`` so a schedule can be
limited to specific weekdays. Existing rows keep ``NULL``, which the scheduler
interprets as "every day", preserving prior behaviour without a data migration.

Revision ID: b7c1f4e9a2d3
Revises: ade8928a2588
Create Date: 2026-10-11 13:10:00.000000
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa

import sacs.models  # noqa: F401


revision = "b7c1f4e9a2d3"
down_revision = "ade8928a2588"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("bell_schedules", schema=None) as batch_op:
        batch_op.add_column(sa.Column("days", sa.JSON(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("bell_schedules", schema=None) as batch_op:
        batch_op.drop_column("days")
