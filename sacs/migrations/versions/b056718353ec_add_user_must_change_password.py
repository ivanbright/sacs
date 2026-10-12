"""add_user_must_change_password

Revision ID: b056718353ec
Revises: b7c1f4e9a2d3
Create Date: 2026-10-12 08:29:17.237404
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = 'b056718353ec'
down_revision = 'b7c1f4e9a2d3'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "users",
        sa.Column(
            "must_change_password",
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
        ),
    )


def downgrade() -> None:
    op.drop_column("users", "must_change_password")
