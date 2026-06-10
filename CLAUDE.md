# ADHD SMS Bot

SMS-based personal assistant for ADHD management. Everything is a **nag** on a single **today list**: you capture items (with `.. `), they nag you toward a deadline with context-aware timing, and you check them off. Also handles Gmail action-item extraction, scheduled basement-light flashes, and morning briefings. Built with FastAPI, PostgreSQL, Twilio, OpenAI GPT-4o, and Gmail IMAP.

> Reminders and exercise tracking were removed — the system is nags-only. The `Reminder`/`ExerciseLog` models and their intents no longer exist; their tables linger as legacy.

## Architecture

Four Docker services (`docker-compose.yaml`):
- **api** (port 8000): FastAPI SMS webhook (`/sms`) receives Twilio POSTs
- **scheduler**: Background loop (every `TICK_SECONDS=60`s) fires due items + Gmail sync every 30min
- **ui** (port 8081): Web dashboard for viewing/deleting items
- **db**: PostgreSQL 16

## Database Tables

| Table | Purpose |
|---|---|
| `nag_schedules` | The core model — every today-list item (user-created + Gmail action items) |
| `scheduled_flashes` | One-time basement-light flashes ("flash lights at 9pm") |
| `pending_confirmations` | Stores confirmation/follow-up requests, incl. `set_deadline` and undo (10-min TTL) |
| `processed_emails` | Tracks Gmail Message-IDs to prevent re-processing |
| `app_state` | Key-value scheduler state (e.g., "briefing_last_sent_date", "last_nag_sent_at" for the global nag gate, "user_context" for the latest location/intent) |
| `sms_log` | Full audit log of all inbound/outbound SMS |
| `daily_checklist_items` | Recurring daily checklist items (added via `##`, auto-reset each local day) |
| `checklists` | Named, one-off checklists (created via `#newlist`) |
| `checklist_items` | Items belonging to a `checklists` row (ordered by `position`) |

Legacy tables still in DB but unused by code: `reminders`, `exercise_log`, `action_items`, `recurring_schedules`.

## Key Concepts

### Light flashes (`app/models.py: ScheduledFlash`)
"flash lights at 9pm" → `flash_lights` intent → a `ScheduledFlash` row with `fire_at`. The
scheduler's `fire_due_flashes` triggers `_flash_basement_light()` (IFTTT webhooks) at the
time, marks it `done`, and sends a short confirmation SMS. This is the only surviving piece
of the old reminder/event-pair light-flash behavior.

### Nags (`app/models.py: NagSchedule`)
Nags are the unified model for both user-created nags and Gmail-extracted action items.

**Two separate timing concepts:**
- **Interval** (`interval_minutes`): How often to nag within ONE cycle (e.g., 15 = every 15 min)
- **Recurrence** (`cron_expression` + `repeating`): How often cycles repeat (e.g., "weekdays at 9am")

**Nag lifecycle (state machine in `fire_due_nags`):**
1. **Dormant** (`active_since=NULL`): Waiting for `next_nag_at` to arrive
2. **Cycle start**: Sets `active_since=now`, `nag_until=now+max_duration_minutes`, `nag_count=0`
3. **Nagging**: Sends message every `interval_minutes`. Count > 1 prepends `(#N)`.
4. **Cycle end** (when `nag_until` passes):
   - If `repeating=True`: Reset to dormant, schedule next cycle via cron
   - If `repeating=False`: Set `status="deleted"` (one-shot, done)

**Deadline-based nags** (`deadline_at` set):
- Nag frequency follows Zeno's paradox: each step waits a **random fraction (0.25–0.5)** of the remaining time, so cadence accelerates toward the deadline but jitters (`scheduler.py: _compute_deadline_interval`)
- Example: 1hr deadline → roughly 15–30min, then again a fraction of what's left, … down to `min_interval_minutes` (default 5 min)
- Past deadline: clamps to `min_interval_minutes`
- Each nag message is GPT-generated with increasing urgency (LOW → MODERATE → HIGH → CRITICAL → OVERDUE)
- One-shot nags start active immediately (`active_since=now` at creation)
- Recurring nags: when a cycle starts at cron time, the **first nag is deferred by a random Zeno step** instead of firing at the exact cron minute, so daily items land at varied times through the day
- Fallback static message on GPT failure

**Global rate gate + coalescing** (`scheduler.py: fire_due_nags`):
- At most **one nag SMS per `GLOBAL_NAG_MIN_GAP` window** (default 5 min), tracked via the `last_nag_sent_at` key in `app_state`
- All nags due in the same window are **coalesced into one SMS**: a single rich message if one item is due, otherwise a numbered list + a short GPT-generated "plan" line (`openai_client.py: generate_nag_plan`)

**Context-aware surfacing** (`app/context_engine.py`):
- The user texts plain location/intent ("heading to Target", "home for the night") → parsed as the `context_update` intent → stored in `app_state` under `user_context`
- `evaluate_context(db)` asks GPT (`openai_client.py: select_relevant_items`) which open today-list items fit the moment (time of day + context + task type) and pulls their `next_nag_at` forward to now, so the gate/coalescer sends them
- Runs immediately on a `context_update` and every ~10 min in the scheduler loop (so loose items still surface without context)

**Quiet hours** (all nags):
- No nags sent between `QUIET_HOURS_START` (default 0 = midnight) and `QUIET_HOURS_END` (default 6 = 6 AM) local time
- If a nag is due during quiet hours, `next_nag_at` is pushed to `QUIET_HOURS_END`
- After quiet hours end, deadline curve naturally computes a shorter interval (catching up)

**Completion-anchored nags** (`anchor_to_completion=True`):
- Next cycle starts relative to when user marks DONE, not the cron schedule
- Uses `cycle_months` or `cycle_days` + `_next_nag_cycle()` with `relativedelta`

**Gmail-sourced nags** (`source="gmail"`):
- Created by `gmail_sync.py` with `interval_minutes=120`, `repeating=False`, `max_duration_minutes=NULL` (nag indefinitely until done)
- `source_ref` stores the email reference string for dedup
- `ProcessedEmail` table tracks Gmail Message-ID headers to prevent re-analyzing emails on restart

### Confirmation Flow
Many actions (cancel, acknowledge, deadline follow-up) go through a two-step confirmation:
1. System fuzzy-matches user text to an item (keyword prefilter → GPT fallback)
2. Creates `PendingConfirmation` with 10-min expiry
3. Sends "Do X? Reply YES to confirm."
4. Next inbound SMS: if starts with "y" → execute; else → decline

### Today list (`.. ` capture)
The today list is a view over nags, not a new table — it's the unified surface for "what do I need to do today."
- **Capture**: text `.. <thing>` (dot-dot-space, prefix-handled in `/sms`). Routed through the nag pipeline (`parse_user_sms("nag me to " + remainder)` → `_handle_create_nag`) so GPT parses any inline deadline/recurrence.
- **Deadline follow-up**: if no deadline/cron is found, the new nag defaults to end-of-day (11pm) and a `PendingConfirmation(action_type="set_deadline")` is created. The next reply is parsed by `_apply_deadline_reply` (`intent_router.py`) — a time sets `deadline_at`, "none"/blank keeps the default.
- **Daily items** = recurring nags on a daily cron — they reappear on the list every day; checking one off ends today's cycle (`_next_nag_cycle` → next day).
- **Check-off**: text `<thing> done` → normal `acknowledge` flow (keyword prefilter → GPT fuzzy match → `execute_acknowledge`). Also a per-item "done" button on the front-page UI (`/nag/done/{id}`).
- **UI**: `/` (the front page) shows "Today's List" (active nags due/scheduled today, via `context_engine.today_items`) above the `##` daily checklist.

### Checklists (`app/models.py: DailyChecklistItem, CheckList, CheckListItem`)
Two separate checklist features, both handled by prefix shortcuts in `/sms` that bypass the GPT intent router (like `#help`).

**Daily checklist** (`##` prefix, `DailyChecklistItem`):
- Text `## <label>` adds a recurring item to the daily checklist
- Items live forever; an item shows as "done" only if its `completed_at` is on today's local date (`ui.py: _is_done_today`), so checks reset each day
- Viewed/toggled on the front-page (`/`) UI, below the today list

**Named lists** (`#newlist` / `#updatelist` prefixes, `CheckList` + `CheckListItem`):
- `#newlist <title>` then one item per subsequent line — creates a `CheckList` with `CheckListItem`s. Title is optional; if blank it defaults to `"List <Mon DD HH:MM AM/PM>"`. Items keep insertion order via `position`.
- `#updatelist` then one item per line — appends items to the most-recently-activated list (`activated_at desc`); errors if no list exists or no items given
- Items are one-off (no daily reset); toggled/deleted via the UI

## SMS Inbound Flow (`app/main.py: /sms`)

```
Twilio POST → /sms
  ├─ From KATHRYN_PHONE (+19739787648)? → Auto-create nag, send confirmation
  ├─ From != USER_PHONE? → Reject
  └─ From == USER_PHONE:
       ├─ Prefix shortcuts (bypass GPT): "#help", "#newlist", "#updatelist", "##", ".. " (capture nag) → handle directly, return
       ├─ PendingConfirmation(set_deadline)? → parse reply → set deadline on the just-created nag, return
       ├─ PendingConfirmation exists? → Handle YES/NO → execute or decline
       └─ No pending confirmation:
            parse_user_sms(Body) via GPT → structured intent + data
            handle_intent(db, parsed) → dispatch to handler → reply SMS
```

## Scheduler Loop (`app/scheduler.py: main()`)

Each tick (60s):
1. `fire_morning_briefing()` — once/day at BRIEFING_TIME
2. `fire_due_flashes()` — trigger any scheduled light flashes whose time has passed
3. `evaluate_context()` — every ~10min: surface context-relevant today-list items (runs before nags so pulled-forward items send this tick)
4. `fire_due_nags()` — nag state machine (cycle start/send/expire) + global 5-min gate + coalescing

Every 30min: `run_gmail_sync()` → fetch emails → GPT extract action items → create nag schedules

On startup: sends recovery notification SMS, runs column migrations.

## Intent Handlers (`app/intent_router.py`)

| Intent | Trigger words | Handler |
|---|---|---|
| `create_nag` | "nag me", "remind me to", "I need to", "bug me"; also the `.. ` capture prefix | `_handle_create_nag` |
| `flash_lights` | "flash lights at 9pm", "blink the lights" | `_handle_flash_lights` |
| `context_update` | plain location/intent ("at the office", "heading to Target") | `_handle_context_update` → stores `user_context`, surfaces relevant items |
| `acknowledge` | "done", "finished", "completed", "<thing> done" | `_handle_acknowledge` → undo confirmation |
| `cancel` | "cancel", "delete", "nevermind", "stop" | `_handle_cancel` → undo confirmation |
| `snooze` | "snooze", "later", "not now" | `_handle_snooze` |
| `list` | "list", "show", "status", "pending" | `_handle_list` |
| `briefing` | "briefing", "what's my day" | `_handle_briefing` |
| `help` | "#help" (prefix, bypasses intent router), "commands" | `_handle_help` |

## Key Files

| File | Purpose |
|---|---|
| `app/main.py` | FastAPI SMS webhook, auto-nag phone handler |
| `app/scheduler.py` | Background loop, all `fire_*` functions, Gmail sync trigger |
| `app/intent_router.py` | All intent handlers, confirmation execution, keyword prefilter, time helpers |
| `app/models.py` | SQLAlchemy models (NagSchedule, ScheduledFlash, PendingConfirmation, ProcessedEmail, etc.) |
| `app/openai_client.py` | GPT intent parsing prompt, action item extraction, fuzzy matching |
| `app/gmail_sync.py` | IMAP fetch, email dedup via ProcessedEmail, creates nag schedules from emails |
| `app/context_engine.py` | Today-list helpers + `evaluate_context` (context-aware surfacing), `user_context` get/set |
| `app/ui.py` | Web dashboard (port 8081) — `/` is the today list, `/lists` checklists, `/nags` raw nags |
| `app/config.py` | All env var loading with file-based fallbacks |
| `app/twilio_client.py` | `send_sms()` wrapper around Twilio REST API |
| `app/morning_briefing.py` | Weather + calendar + market briefing generation |
| `app/database.py` | SQLAlchemy engine, session factory, Base |

## Configuration (`app/config.py`)

All config is via environment variables with sensible defaults. Credentials fall back to reading from files in `/home/iray/`.

Key settings: `DATABASE_URL`, `OPENAI_API_KEY`, `TWILIO_*`, `USER_PHONE`, `USER_TIMEZONE`, `TICK_SECONDS`, `GMAIL_*`, `WEATHERAPI_KEY`, `BRIEFING_TIME`, `BASEMENT_LIGHT_ON/OFF`, `QUIET_HOURS_START`, `QUIET_HOURS_END`, `DEFAULT_MIN_INTERVAL`, `DEFAULT_MAX_INTERVAL`, `GLOBAL_NAG_MIN_GAP` (global floor between outbound nag SMS, default 5 min). (`EXERCISE_*_TIME` remain in config but are unused.)

## Development Notes

- Database migrations are done inline in `scheduler.py:main()` using `ALTER TABLE ... ADD COLUMN IF NOT EXISTS` (PostgreSQL)
- `_keyword_prefilter()` tries fast substring matching before calling GPT for acknowledge/cancel — saves API calls
- `with_for_update(skip_locked=True)` used in scheduler queries to prevent double-firing
- `_random_nag_time()` picks a random 9am-5pm time when user doesn't specify one
- Auto-nag phone (`+19739787648`) allows external systems to create nags at 2-hour intervals by texting
- Every inbound SMS from the user hits OpenAI for intent parsing; no local pre-parsing (except prefix shortcuts: `.. `, `#help`, `#newlist`, `#updatelist`, `##`, `kk`)
