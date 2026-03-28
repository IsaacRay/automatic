-- Migration: Merge recurring_schedules into reminders
-- Run with: sudo docker compose exec -T db psql -U adhdbot -d adhdbot < migrate_recurring_to_reminders.sql

BEGIN;

-- 1. Add new columns to reminders (IF NOT EXISTS for safety)
ALTER TABLE reminders ADD COLUMN IF NOT EXISTS cron_expression VARCHAR(100);
ALTER TABLE reminders ADD COLUMN IF NOT EXISTS timezone VARCHAR(50) DEFAULT 'America/New_York';

-- 2. Migrate active recurring_schedules to reminders
INSERT INTO reminders (
    user_phone, label, fire_at, message, cron_expression, timezone, status, created_at
)
SELECT
    user_phone,
    label,
    next_fire_at,                    -- fire_at = next scheduled fire
    'Reminder: ' || label,           -- static message (no more GPT generation)
    cron_expression,                 -- preserve cron
    timezone,                        -- preserve timezone
    'pending',                       -- status = pending (ready to fire)
    created_at                       -- preserve original created_at
FROM recurring_schedules
WHERE status = 'active';

-- 3. Mark migrated recurring_schedules as deleted
UPDATE recurring_schedules SET status = 'deleted' WHERE status = 'active';

-- 4. Report results
SELECT 'Migrated recurring schedules:' AS info, count(*) AS count
FROM reminders WHERE cron_expression IS NOT NULL;

SELECT 'Remaining active recurring_schedules:' AS info, count(*) AS count
FROM recurring_schedules WHERE status = 'active';

COMMIT;
