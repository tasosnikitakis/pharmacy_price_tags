"""Add tag.firmware: the firmware version a tag reports during discovery.

Revision ID: 0002
Revises: 0001
Create Date: 2026-11-18
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("tag", sa.Column("firmware", sa.String(length=64), nullable=True))


def downgrade() -> None:
    op.drop_column("tag", "firmware")
