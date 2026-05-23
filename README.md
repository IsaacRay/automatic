# ADHD SMS Bot

SMS-based personal assistant for ADHD management — reminders (one-time and recurring), nags, Gmail action item extraction, exercise tracking, and morning briefings. Built with FastAPI, PostgreSQL, Twilio, OpenAI GPT-4o, and Gmail IMAP.

## Architecture

Four Docker services (`docker-compose.yaml`):
- **api** (port 8000): FastAPI SMS webhook (`/sms`) receives Twilio POSTs
- **scheduler**: Background loop (every `TICK_SECONDS=60`s) fires due items + Gmail sync every 30min
- **ui** (port 8081): Web dashboard for viewing/deleting items
- **db**: PostgreSQL 16

## Database Tables

| Table | Purpose |
|---|---|
| `reminders` | One-time and recurring reminders (cron_expression set = recurring) |
| `nag_schedules` | Repeated nags at intervals until acknowledged (also used for Gmail action items) |
| `pending_confirmations` | Stores YES/NO confirmation requests (10-min TTL) |
| `processed_emails` | Tracks Gmail Message-IDs to prevent re-processing |
| `app_state` | Key-value scheduler state (e.g., "briefing_last_sent_date") |
| `exercise_log` | User exercise activity logs |
| `sms_log` | Full audit log of all inbound/outbound SMS |

Legacy tables still in DB but unused by code: `action_items`, `recurring_schedules`.

## Key Concepts

### Reminders (`app/models.py: Reminder`)
Unified model for both one-time and recurring reminders.

**One-time reminders** (`cron_expression=NULL`):
- Fire once at `fire_at` time, status goes `pending` → `sent`
- **Event pairs**: Two reminders share a `parent_event_id` — a prep reminder 30min before and an event-time reminder. Reschedule/cancel/acknowledge operates on both.
- Event-time reminder triggers basement light flash (IFTTT webhooks).

**Recurring reminders** (`cron_expression` set):
- Fire at `fire_at`, then after sending, recompute `fire_at` from cron and reset `status="pending"`
- No event pairs — single reminder with static message
- Always use static message text (no GPT generation)
- Example: "Remind me about Dr Watson every Tuesday at 3pm" → `cron_expression="0 15 * * 2"`

### Nags (`app/models.py: NagSchedule`)
Nags are the unified model for user-created nags and Gmail-extracted action items. **Every nag is a deadline nag** — frequency always comes from the Zeno curve off `deadline_at`, never from a fixed interval.

**Two shapes:**

*One-shot* (`repeating=False`, `cron_expression=NULL`):
- `active_since=now` (or `NULL` if the user said "start at Z" — stays dormant until `next_nag_at`)
- `deadline_at` is absolute; defaults to 11pm local today if user doesn't specify
- Nags forever past the deadline at `min_interval_minutes` (no auto-delete — user must ack)

*Recurring* (`repeating=True`, `cron_expression` set):
- `cron_expression` schedules each cycle's START
- `deadline_offset_minutes` = minutes from cycle start to that cycle's deadline; defaults to "minutes until 11pm local on the cycle-start day" if not specified
- At cycle start, scheduler sets `active_since=now`, computes `deadline_at = active_since + deadline_offset_minutes`
- Past the deadline, keeps nagging at `min_interval_minutes` until the user acks (no auto cycle-end)
- On ack, cycle resets to dormant and the next `next_nag_at` is scheduled via cron (or via anchor if `anchor_to_completion`)
- `anchor_to_completion=True` + `cycle_months`/`cycle_days`: next cycle starts relative to user ack instead of cron

**Nag lifecycle (state machine in `fire_due_nags`):**
1. **Dormant** (`active_since=NULL`): waiting for `next_nag_at` to arrive
2. **Cycle start**: sets `active_since=now`, `nag_count=0`. For recurring, also sets `deadline_at = now + deadline_offset_minutes`
3. **Nagging**: Zeno curve (`_compute_deadline_interval`) computes next interval; count > 1 prepends `(#N)`
4. **Past deadline**: keeps nagging at `min_interval_minutes` indefinitely (one-shot and recurring alike) — no auto cycle-end
5. **Ack**: user replies DONE — one-shot deletes, recurring resets to dormant (next cycle via cron or anchor)

**Zeno's paradox curve** (`_compute_deadline_interval`):
- interval = 1/3 of remaining time to `deadline_at`, clamped to `min_interval_minutes` (default 5)
- Past deadline: clamps to floor and keeps firing
- Each message is GPT-generated with urgency tier (LOW → MODERATE → HIGH → CRITICAL → OVERDUE); static fallback on GPT failure

**Quiet hours** (all nags): no nags between `QUIET_HOURS_START` and `QUIET_HOURS_END` local time; `next_nag_at` gets pushed to `QUIET_HOURS_END`.

**Gmail-sourced nags** (`source="gmail"`):
- Created by `gmail_sync.py` as one-shot deadline nags (`cron_expression=NULL`, `active_since=now`, `deadline_at=11pm today`)
- Nag indefinitely after deadline at floor interval until user acks
- `source_ref` stores the email reference for dedup; `ProcessedEmail` tracks Message-IDs

### Confirmation Flow
Many actions (reschedule, cancel, acknowledge) go through a two-step confirmation:
1. System fuzzy-matches user text to an item (keyword prefilter → GPT fallback)
2. Creates `PendingConfirmation` with 10-min expiry
3. Sends "Do X? Reply YES to confirm."
4. Next inbound SMS: if starts with "y" → execute; else → decline

## SMS Inbound Flow (`app/main.py: /sms`)

```
Twilio POST → /sms
  ├─ From KATHRYN_PHONE (+19739787648)? → Auto-create nag, send confirmation
  ├─ From != USER_PHONE? → Reject
  └─ From == USER_PHONE:
       ├─ PendingConfirmation exists? → Handle YES/NO → execute or decline
       └─ No pending confirmation:
            parse_user_sms(Body) via GPT → structured intent + data
            handle_intent(db, parsed) → dispatch to handler → reply SMS
```

## Scheduler Loop (`app/scheduler.py: main()`)

Each tick (60s):
1. `fire_morning_briefing()` — once/day at BRIEFING_TIME
2. `fire_exercise_morning()` — once/day at EXERCISE_MORNING_TIME
3. `fire_exercise_evening()` — once/day at EXERCISE_EVENING_TIME
4. `fire_due_reminders()` — all pending reminders with `fire_at <= now` (recurring ones reschedule themselves)
5. `fire_due_nags()` — nag state machine (cycle start/send/expire)

Every 30min: `run_gmail_sync()` → fetch emails → GPT extract action items → create nag schedules

On startup: sends recovery notification SMS, runs column migrations.

## Intent Handlers (`app/intent_router.py`)

| Intent | Trigger words | Handler |
|---|---|---|
| `create_reminder` | time-based phrases ("at 4pm", "friday", "every Tuesday at 3pm") | `_handle_create_reminder` |
| `create_nag` | "nag me", "keep reminding", "bug me", "pester" | `_handle_create_nag` |
| `reschedule` | "move", "reschedule", "push to", "change to" | `_handle_reschedule` → confirmation |
| `acknowledge` | "done", "finished", "completed" | `_handle_acknowledge` → confirmation |
| `cancel` | "cancel", "delete", "nevermind", "stop" | `_handle_cancel` → confirmation |
| `snooze` | "snooze", "later", "not now" | `_handle_snooze` |
| `list` | "list", "show", "status", "pending" | `_handle_list` |
| `briefing` | "briefing", "what's my day" | `_handle_briefing` |
| `log_exercise` | "I ran", "I biked", "went for a walk" | `_handle_log_exercise` |
| `exercise_history` | "exercise history", "my workouts" | `_handle_exercise_history` |
| `help` | "commands", "info" | `_handle_help` |

## Key Files

| File | Purpose |
|---|---|
| `app/main.py` | FastAPI SMS webhook, auto-nag phone handler |
| `app/scheduler.py` | Background loop, all `fire_*` functions, Gmail sync trigger |
| `app/intent_router.py` | All intent handlers, confirmation execution, keyword prefilter, time helpers |
| `app/models.py` | SQLAlchemy models (Reminder, NagSchedule, PendingConfirmation, ProcessedEmail, etc.) |
| `app/openai_client.py` | GPT intent parsing prompt, action item extraction, fuzzy matching |
| `app/gmail_sync.py` | IMAP fetch, email dedup via ProcessedEmail, creates nag schedules from emails |
| `app/ui.py` | Web dashboard (port 8081) |
| `app/config.py` | All env var loading with file-based fallbacks |
| `app/twilio_client.py` | `send_sms()` wrapper around Twilio REST API |
| `app/morning_briefing.py` | Weather + calendar + market briefing generation |
| `app/exercise_motivation.py` | Morning/evening exercise motivation messages |
| `app/database.py` | SQLAlchemy engine, session factory, Base |

## Configuration (`app/config.py`)

All config is via environment variables with sensible defaults. Credentials fall back to reading from files in `/home/iray/`.

Key settings: `DATABASE_URL`, `OPENAI_API_KEY`, `TWILIO_*`, `USER_PHONE`, `USER_TIMEZONE`, `TICK_SECONDS`, `GMAIL_*`, `WEATHERAPI_KEY`, `BRIEFING_TIME`, `EXERCISE_*_TIME`, `BASEMENT_LIGHT_ON/OFF`, `QUIET_HOURS_START`, `QUIET_HOURS_END`, `DEFAULT_MIN_INTERVAL`, `DEFAULT_MAX_INTERVAL`.

## Development Notes

- Database migrations are done inline in `scheduler.py:main()` using `ALTER TABLE ... ADD COLUMN IF NOT EXISTS` (PostgreSQL)
- `_keyword_prefilter()` tries fast substring matching before calling GPT for acknowledge/cancel — saves API calls
- `with_for_update(skip_locked=True)` used in scheduler queries to prevent double-firing
- `_random_nag_time()` picks a random 9am-5pm time when user doesn't specify one
- Auto-nag phone (`+19739787648`) allows external systems to create nags at 2-hour intervals by texting
- Every inbound SMS from the user hits OpenAI for intent parsing; no local pre-parsing
- Recurring reminders use static message text — no GPT generation at fire time
