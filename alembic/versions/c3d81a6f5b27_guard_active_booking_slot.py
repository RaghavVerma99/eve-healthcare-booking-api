"""Guard against double-booking a centre slot

Revision ID: c3d81a6f5b27
Revises: 7c1f4a9b2d38
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "c3d81a6f5b27"
down_revision: str | None = "7c1f4a9b2d38"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

ACTIVE_PREDICATE = "status IN ('PENDING', 'CONFIRMED')"


def upgrade() -> None:
    """Add the partial unique index that the application-level slot check cannot.

    `create_booking` reads for a conflicting slot and then inserts, so two
    concurrent requests can both observe a free slot and both commit. The read
    stays, because it produces a friendlier 409, but this index is what makes
    the invariant true.

    Duplicates that the old code already allowed are resolved first, keeping the
    lowest-id live booking per slot. A duplicate that carries payment history is
    never deleted: `payments.booking_id` is ON DELETE RESTRICT, and discarding a
    financial record to satisfy a migration is not a call this script should
    make silently. Those rows abort the upgrade with an explicit message.
    """
    duplicates = op.get_bind().execute(
        sa.text(
            """
            SELECT b.id, b.reference, b.centre_id, b.appointment_at,
                   (SELECT COUNT(*) FROM payments p WHERE p.booking_id = b.id) AS payment_count
            FROM bookings b
            JOIN (
                SELECT centre_id, appointment_at
                FROM bookings
                WHERE status IN ('PENDING', 'CONFIRMED')
                GROUP BY centre_id, appointment_at
                HAVING COUNT(*) > 1
            ) dupes
              ON dupes.centre_id = b.centre_id
             AND dupes.appointment_at = b.appointment_at
            WHERE b.status IN ('PENDING', 'CONFIRMED')
              AND b.id NOT IN (
                    SELECT MIN(id) FROM bookings
                    WHERE status IN ('PENDING', 'CONFIRMED')
                    GROUP BY centre_id, appointment_at
              )
            """
        )
    ).fetchall()

    unsafe = [row for row in duplicates if row.payment_count]
    if unsafe:
        refs = ", ".join(sorted(str(row.reference) for row in unsafe))
        raise RuntimeError(
            "Cannot add uq_bookings_active_slot: these duplicate-slot bookings already "
            f"have payment history and will not be deleted automatically: {refs}. "
            "Cancel or re-time them, then re-run the migration."
        )
    if duplicates:
        op.execute(
            """
            DELETE FROM bookings
            WHERE status IN ('PENDING', 'CONFIRMED')
              AND id NOT IN (
                    SELECT MIN(id) FROM bookings
                    WHERE status IN ('PENDING', 'CONFIRMED')
                    GROUP BY centre_id, appointment_at
              )
            """
        )

    predicate = sa.text(ACTIVE_PREDICATE)
    op.create_index(
        "uq_bookings_active_slot",
        "bookings",
        ["centre_id", "appointment_at"],
        unique=True,
        postgresql_where=predicate,
        sqlite_where=predicate,
    )


def downgrade() -> None:
    op.drop_index("uq_bookings_active_slot", table_name="bookings")
