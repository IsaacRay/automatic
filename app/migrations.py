"""Database migrations — idempotent ALTER TABLE statements run on service startup.

Imported by both api (main.py) and scheduler (scheduler.py) so whichever boots
first applies the schema, and the other won't crash on INSERTs against stale columns.
"""

import logging
from sqlalchemy import text

from app.database import engine, Base

log = logging.getLogger(__name__)


def _column_exists(conn, table: str, column: str) -> bool:
    row = conn.execute(text("""
        SELECT 1 FROM information_schema.columns
         WHERE table_name = :t AND column_name = :c
    """), {"t": table, "c": column}).fetchone()
    return row is not None


def run_migrations():
    """Apply inline schema migrations. Safe to re-run."""
    Base.metadata.create_all(engine)

    with engine.connect() as conn:
        try:
            conn.execute(text(
                "ALTER TABLE nag_schedules ADD COLUMN IF NOT EXISTS repeating BOOLEAN NOT NULL DEFAULT false"
            ))
            conn.execute(text(
                "ALTER TABLE nag_schedules ADD COLUMN IF NOT EXISTS recurrence_description VARCHAR(200)"
            ))
            conn.execute(text(
                "ALTER TABLE nag_schedules ADD COLUMN IF NOT EXISTS source VARCHAR(50)"
            ))
            conn.execute(text(
                "ALTER TABLE nag_schedules ADD COLUMN IF NOT EXISTS source_ref TEXT"
            ))
            conn.execute(text(
                "ALTER TABLE nag_schedules ADD COLUMN IF NOT EXISTS completed_at TIMESTAMPTZ"
            ))
            conn.execute(text(
                "ALTER TABLE nag_schedules ADD COLUMN IF NOT EXISTS deadline_at TIMESTAMPTZ"
            ))
            conn.execute(text(
                "ALTER TABLE nag_schedules ADD COLUMN IF NOT EXISTS min_interval_minutes INTEGER"
            ))
            conn.execute(text(
                "ALTER TABLE nag_schedules ADD COLUMN IF NOT EXISTS deadline_offset_minutes INTEGER"
            ))
            conn.execute(text(
                "ALTER TABLE nag_schedules ADD COLUMN IF NOT EXISTS snooze_count INTEGER NOT NULL DEFAULT 0"
            ))
            conn.execute(text("ALTER TABLE nag_schedules DROP COLUMN IF EXISTS max_interval_minutes"))
            conn.execute(text("ALTER TABLE sms_log DROP COLUMN IF EXISTS related_type"))
            conn.execute(text("ALTER TABLE sms_log DROP COLUMN IF EXISTS related_id"))
            conn.execute(text("ALTER TABLE processed_emails DROP COLUMN IF EXISTS subject"))
            conn.execute(text("ALTER TABLE processed_emails DROP COLUMN IF EXISTS date"))

            # Backfill deadline_offset_minutes from max_duration_minutes for recurring nags
            # (only if max_duration_minutes is still around — skip on re-runs)
            if _column_exists(conn, "nag_schedules", "max_duration_minutes"):
                conn.execute(text("""
                    UPDATE nag_schedules
                       SET deadline_offset_minutes = max_duration_minutes
                     WHERE deadline_offset_minutes IS NULL
                       AND max_duration_minutes IS NOT NULL
                       AND repeating = true
                """))
            # Backfill deadline_at for currently-active recurring cycles
            conn.execute(text("""
                UPDATE nag_schedules
                   SET deadline_at = active_since + (COALESCE(deadline_offset_minutes, 720) || ' minutes')::interval
                 WHERE deadline_at IS NULL
                   AND active_since IS NOT NULL
                   AND repeating = true
                   AND status = 'active'
            """))
            # Backfill deadline_at for non-repeating active nags without one
            conn.execute(text("""
                UPDATE nag_schedules
                   SET deadline_at = (date_trunc('day', created_at AT TIME ZONE timezone)
                                       + interval '23 hours') AT TIME ZONE timezone
                 WHERE deadline_at IS NULL
                   AND repeating = false
                   AND status = 'active'
            """))

            conn.execute(text("ALTER TABLE nag_schedules DROP COLUMN IF EXISTS interval_minutes"))
            conn.execute(text("ALTER TABLE nag_schedules DROP COLUMN IF EXISTS max_duration_minutes"))
            conn.execute(text("ALTER TABLE nag_schedules DROP COLUMN IF EXISTS nag_until"))
            conn.execute(text("ALTER TABLE nag_schedules ALTER COLUMN cron_expression DROP NOT NULL"))
            conn.commit()
            log.info("Migrations applied successfully")
        except Exception:
            log.exception("Migration failed")
            raise
