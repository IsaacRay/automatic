-- Migration: Merge action_items into nag_schedules
-- Run with: docker compose exec -T db psql -U adhdbot -d adhdbot < migrate_actions_to_nags.sql

BEGIN;

-- 1. Add new columns to nag_schedules (IF NOT EXISTS for safety)
ALTER TABLE nag_schedules ADD COLUMN IF NOT EXISTS source VARCHAR(50);
ALTER TABLE nag_schedules ADD COLUMN IF NOT EXISTS source_ref TEXT;
ALTER TABLE nag_schedules ADD COLUMN IF NOT EXISTS completed_at TIMESTAMPTZ;

-- 2. Create processed_emails table
CREATE TABLE IF NOT EXISTS processed_emails (
    id SERIAL PRIMARY KEY,
    message_id VARCHAR(255) NOT NULL UNIQUE,
    subject TEXT,
    date VARCHAR(100),
    processed_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS ix_processed_emails_message_id ON processed_emails (message_id);

-- 3. Migrate pending action_items to nag_schedules with 1 hour interval
INSERT INTO nag_schedules (
    user_phone, label, message, cron_expression, interval_minutes,
    max_duration_minutes, timezone, next_nag_at, nag_until,
    active_since, nag_count, repeating, anchor_to_completion,
    recurrence_description, source, source_ref, status, created_at
)
SELECT
    user_phone,
    description,                              -- label = description
    'Action item: ' || description,           -- message
    '0 9 * * *',                              -- cron_expression (placeholder)
    60,                                       -- interval_minutes = 1 hour
    NULL,                                     -- no max_duration (nag until done)
    'America/New_York',                       -- timezone
    COALESCE(next_remind_at, NOW()),          -- next_nag_at
    NULL,                                     -- nag_until
    NOW(),                                    -- active_since (start nagging immediately)
    remind_count,                             -- preserve nag_count
    false,                                    -- not repeating (one-shot)
    false,                                    -- not anchor_to_completion
    NULL,                                     -- no recurrence_description
    source,                                   -- preserve source
    source_ref,                               -- preserve source_ref
    'active',                                 -- status
    created_at                                -- preserve original created_at
FROM action_items
WHERE status = 'pending';

-- 4. Mark migrated action_items as archived (keep for reference, don't delete)
UPDATE action_items SET status = 'archived' WHERE status = 'pending';

-- 5. Report results
SELECT 'Migrated action items:' AS info, count(*) AS count
FROM nag_schedules WHERE source IS NOT NULL;

SELECT 'Remaining pending action items:' AS info, count(*) AS count
FROM action_items WHERE status = 'pending';

COMMIT;
