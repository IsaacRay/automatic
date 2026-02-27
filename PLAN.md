# Plan: SMS-Based ADHD Assistant

## Context

The project currently has a standalone `gmail_reader.py` that fetches emails, extracts action items via OpenAI, and sends a single SMS blob via Twilio. The Docker infrastructure (PostgreSQL + FastAPI + scheduler) is defined but the `app/` module doesn't exist yet.

The goal is to build an interactive SMS bot where the user texts natural language commands (e.g., "I have a meeting at 4pm Friday about Esub Lambdas") and the system autonomously schedules reminders, recurring messages, and tracks action items until acknowledged.

## File Structure

Create the `app/` module:

```
app/
├── __init__.py           # empty package marker
├── config.py             # settings: env vars with file-based fallback
├── database.py           # SQLAlchemy engine, session, Base
├── models.py             # all DB tables
├── schemas.py            # Pydantic models for OpenAI response validation
├── openai_client.py      # raw HTTP calls to OpenAI (matching existing pattern)
├── twilio_client.py      # send SMS (centralized from gmail_reader.py)
├── intent_router.py      # map parsed intents → DB operations + reply text
├── scheduler.py          # background loop: fire due reminders/recurring/action items
└── gmail_sync.py         # refactored gmail_reader: store action items in DB
```

Also update: `requirements.txt`, `docker-compose.yaml`, `gmail_reader.py` (thin wrapper)

## Database Schema (4 tables)

All timestamps stored as UTC. User timezone: `America/New_York`.

### `reminders` — one-time reminders
| Column | Type | Notes |
|--------|------|-------|
| id | Integer PK | |
| user_phone | String(20), indexed | |
| label | Text | "Meeting about Esub Lambdas" |
| fire_at | DateTime(tz), indexed | when to send (UTC) |
| message | Text | actual SMS text |
| parent_event_id | String(64), nullable | groups related reminders (prep + start) |
| status | String(20) | pending → sent → dismissed |
| sent_at | DateTime(tz), nullable | |
| created_at | DateTime(tz) | server_default=now() |

### `recurring_schedules` — repeated messages
| Column | Type | Notes |
|--------|------|-------|
| id | Integer PK | |
| user_phone | String(20), indexed | |
| label | Text | "Daily exercise encouragement" |
| message_prompt | Text | prompt sent to OpenAI each firing for a fresh message |
| cron_expression | String(100) | e.g. "0 17 * * *" |
| timezone | String(50) | default "America/New_York" |
| next_fire_at | DateTime(tz), indexed | precomputed, avoids parsing cron every tick |
| status | String(20) | active / paused / deleted |
| created_at | DateTime(tz) | |

### `action_items` — from Gmail or SMS, nagged until acknowledged
| Column | Type | Notes |
|--------|------|-------|
| id | Integer PK | |
| user_phone | String(20), indexed | |
| source | String(50) | "gmail" or "sms" |
| source_ref | Text, nullable | email subject/date for traceability |
| description | Text | what needs to be done |
| status | String(20) | pending → done / snoozed |
| remind_count | Integer | tracks escalation |
| next_remind_at | DateTime(tz), indexed | precomputed with backoff |
| snooze_until | DateTime(tz), nullable | |
| created_at | DateTime(tz) | |
| completed_at | DateTime(tz), nullable | |

Backoff schedule: immediate → 4h → 24h → every 48h thereafter.

### `sms_log` — audit trail
| Column | Type | Notes |
|--------|------|-------|
| id | Integer PK | |
| direction | String(10) | "inbound" / "outbound" |
| phone | String(20) | |
| body | Text | |
| twilio_sid | String(64), nullable | |
| related_type | String(30), nullable | "reminder" / "recurring" / "action_item" |
| related_id | Integer, nullable | |
| created_at | DateTime(tz) | |

Tables created via `Base.metadata.create_all(engine)` on startup (no Alembic for initial build).

## Core Components

### 1. `app/config.py` — Configuration
- Try env var first, fall back to reading credential files from `/home/iray/`
- Settings: DATABASE_URL, OPENAI_API_KEY, TWILIO_ACCOUNT_SID/TOKEN/FROM_NUMBER, USER_PHONE (`+15184690834`), USER_TIMEZONE (`America/New_York`), TICK_SECONDS, Gmail creds

### 2. `app/openai_client.py` — Two functions

**`parse_user_sms(message) -> dict`**: The core intelligence. System prompt instructs GPT-4o to return structured JSON with:
- `intent`: one of `create_reminder`, `create_recurring`, `acknowledge`, `snooze`, `list`, `help`, `unknown`
- `data`: intent-specific fields (datetime, cron expression, label, messages, etc.)

Key prompt details:
- Injects current date/time so GPT resolves "Friday", "tomorrow", etc.
- For meetings: auto-generates prep reminder (30 min before) + event reminder
- For recurring: generates cron expression + a `message_prompt` for varied messages
- Uses `response_format: {"type": "json_object"}` for reliable parsing
- Low temperature (0.3) for structured output consistency

**`generate_recurring_message(prompt) -> str`**: Called by scheduler each firing. High temperature (0.9) for variety. Instructed to stay under 160 chars.

### 3. `app/main.py` — FastAPI webhook

Single endpoint: `POST /sms`
1. Parse Twilio form data (`From`, `Body`, `MessageSid`)
2. **Reject if `From` is not `+15184690834`** — return empty TwiML immediately, skip all processing. Only this number can interact with the bot.
3. Log inbound SMS
4. Call `parse_user_sms()` → get structured intent
5. Call `handle_intent()` → DB operations + reply text
6. Send reply via Twilio REST API
7. Return empty TwiML `<Response></Response>`

Also: `GET /health` for monitoring.

### 4. `app/intent_router.py` — Business logic

Maps each intent to DB operations:
- **create_reminder**: Creates 1-2 `Reminder` rows (prep + event) linked by `parent_event_id`. Replies with confirmation.
- **create_recurring**: Creates `RecurringSchedule` row with precomputed `next_fire_at`. Replies with human-readable schedule summary.
- **acknowledge**: Marks matching reminders/action items as done/dismissed. Supports "done", "done all", keyword matching. Dismissing one reminder also dismisses siblings (same `parent_event_id`).
- **snooze**: Delays `next_remind_at` by specified duration (default 1 hour).
- **list**: Queries pending reminders, action items, active recurring schedules. Formats as SMS.
- **help**: Returns command reference.

### 5. `app/scheduler.py` — Background loop

Runs as `python -m app.scheduler`. Every 60 seconds:

1. **Fire due reminders**: Query `reminders WHERE status='pending' AND fire_at <= now()`. Send SMS, set status to "sent". Always send even if late (late reminder > no reminder for ADHD).

2. **Fire due recurring**: Query `recurring_schedules WHERE status='active' AND next_fire_at <= now()`. Call OpenAI to generate fresh message, send SMS, compute and store next `next_fire_at` using croniter.

3. **Fire action item re-reminders**: Query `action_items WHERE status='pending' AND next_remind_at <= now()`. Send reminder SMS, increment `remind_count`, compute next `next_remind_at` using backoff schedule.

4. **Gmail sync** (every 30 minutes): Fetch recent emails, extract structured action items via OpenAI, dedup against existing rows, store new ones with `next_remind_at = now()`.

Uses `FOR UPDATE SKIP LOCKED` to prevent double-sends.

### 6. `app/gmail_sync.py` — Refactored Gmail reader

Reuses IMAP logic from `gmail_reader.py` but:
- Modified OpenAI prompt returns structured JSON array of individual action items (not free-text blob)
- Each item stored as an `ActionItem` row with `next_remind_at = now()`
- Deduplication by `source_ref` + `description`
- Scheduler picks them up and nags until user replies "done"

`gmail_reader.py` becomes a thin wrapper importing from `app.gmail_sync`.

## Updated Dependencies (`requirements.txt`)

Add:
- `croniter` — cron expression parsing (small, no transitive deps)
- `python-multipart` — required by FastAPI to parse form data from Twilio webhooks

## Updated `docker-compose.yaml`

- Replace NTFY env vars with Twilio/OpenAI/Gmail env vars
- Use `${VAR}` syntax to read from `.env` file (gitignored)
- Both api and scheduler services get all credential env vars
- Scheduler additionally gets Gmail-specific vars

## Twilio Setup (manual step)

Configure the Twilio phone number's messaging webhook to `POST https://<public-url>/sms`. For local dev, use ngrok: `ngrok http 8000`.

## Implementation Order

1. `app/__init__.py` + `app/config.py` + `app/database.py` — foundation
2. `app/models.py` — all 4 tables, verify with create_all
3. `app/twilio_client.py` — centralized send_sms from gmail_reader.py
4. `app/openai_client.py` — parse_user_sms + generate_recurring_message
5. `app/schemas.py` — Pydantic models for intent validation
6. `app/intent_router.py` — all intent handlers
7. `app/main.py` — FastAPI webhook endpoint
8. `app/scheduler.py` — background tick loop
9. `app/gmail_sync.py` — refactored Gmail reader
10. Update `gmail_reader.py` — thin wrapper
11. Update `requirements.txt` + `docker-compose.yaml`

## Verification

1. **Unit test intent parsing**: Call `parse_user_sms` with sample messages, verify JSON structure
2. **Local webhook test**: `curl -X POST http://localhost:8000/sms -d "From=+15184690834&Body=meeting at 4pm friday about lambdas"` — verify reminder rows created and confirmation SMS received
3. **Scheduler test**: Insert a reminder with `fire_at` in the past, start scheduler, verify SMS sent within 60s
4. **Recurring test**: Create a recurring schedule with `next_fire_at` in the past, verify fresh motivational message generated and sent
5. **Acknowledge test**: Send "done" via SMS, verify most recent item marked complete
6. **Gmail sync test**: Run sync, verify action items appear in DB and reminders start arriving
7. **Docker integration**: `docker compose up --build`, send real SMS through Twilio, verify full round-trip
