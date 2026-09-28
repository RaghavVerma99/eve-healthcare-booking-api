"""Distinguish "signature not checked" from "signature rejected"

Revision ID: b47e0c93da18
Revises: c3d81a6f5b27
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "b47e0c93da18"
down_revision: str | None = "c3d81a6f5b27"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Make webhook_events.signature_valid nullable.

    The column was NOT NULL and every unsigned delivery was recorded as False,
    which is indistinguishable from a rejected signature. That made the admin
    inspector report tampering where none happened, and stopped the replay task
    from re-processing any event received while checking was disabled.

    A NULL now means "not checked". Existing rows cannot be told apart, so they
    are reset to NULL: that is the accurate reading for a store whose receiver
    only accepted signed deliveries in the first place, and a replay of any of
    them is re-verified by the receiver anyway.
    """
    with op.batch_alter_table("webhook_events") as batch:
        batch.alter_column(
            "signature_valid",
            existing_type=sa.Boolean(),
            nullable=True,
        )
    # FALSE/TRUE rather than 0/1: PostgreSQL has no boolean = integer operator
    # and would fail this statement outright.
    op.execute(
        "UPDATE webhook_events SET signature_valid = NULL WHERE signature_valid IS FALSE"
    )


def downgrade() -> None:
    # Rows that are NULL now have no meaningful True/False value, so collapse
    # them to True (checked and accepted) rather than failing on NULL.
    op.execute(
        "UPDATE webhook_events SET signature_valid = TRUE WHERE signature_valid IS NULL"
    )
    with op.batch_alter_table("webhook_events") as batch:
        batch.alter_column(
            "signature_valid",
            existing_type=sa.Boolean(),
            nullable=False,
        )
