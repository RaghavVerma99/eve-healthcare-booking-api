"""Add refresh token family to enable reuse detection

Revision ID: 7c1f4a9b2d38
Revises: 25d85ba4cc0f
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "7c1f4a9b2d38"
down_revision: str | None = "25d85ba4cc0f"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "refresh_tokens",
        sa.Column("family_id", sa.String(length=64), nullable=True),
    )
    # Every pre-existing token becomes the root of its own family. Each token
    # that has already been rotated past is revoked, so seeding it as a lone
    # family can never revoke an unrelated live session on upgrade.
    op.execute("UPDATE refresh_tokens SET family_id = jti WHERE family_id IS NULL")
    # SQLite cannot ALTER a column to NOT NULL in place, so recreate the table
    # there. batch_alter_table does this transparently and is a no-op on
    # PostgreSQL.
    with op.batch_alter_table("refresh_tokens") as batch:
        batch.alter_column("family_id", existing_type=sa.String(length=64), nullable=False)
    op.create_index(
        op.f("ix_refresh_tokens_family_id"), "refresh_tokens", ["family_id"], unique=False
    )


def downgrade() -> None:
    op.drop_index(op.f("ix_refresh_tokens_family_id"), table_name="refresh_tokens")
    with op.batch_alter_table("refresh_tokens") as batch:
        batch.drop_column("family_id")
