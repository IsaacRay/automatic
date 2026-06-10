# Redesign: everything operates via the "today list"

## Context

Today the bot is nag-centric: you say "nag me about X, deadline Y" and the engine
escalates reminders on a fixed Zeno curve (`interval = remaining/3`). The goal is to
reframe the system around a **today list**: a single list of items that each have a
deadline, surfaced and re-nagged throughout the day until checked off.

Key behavior changes:

1. **`.. ` capture** — texting `.. <thing>` adds an item, parses a deadline inline if
   present, otherwise asks for one (next reply sets it). All items have a deadline
   (default = end of day today, 11pm, which `_handle_create_nag` already does).
2. **Daily items live on the list every day** — these are just `repeating=True` nags
   on a daily cron; checking one off ends *today's* cycle and it returns tomorrow.
3. **Context-aware timing** — daily/loose items should nag "somewhat randomly through
   the day," with an LLM choosing *when* based on (a) time of day, (b) a location/intent
   SMS the user sends ("heading to Target"), (c) task type.
4. **Check-off by text** — `<thing> done` fuzzy-matches and stops nags for the day
   (reuses the existing acknowledge flow). Plain location SMS updates context.
5. **Calmer Zeno + no spam** — fraction randomized in [0.25, 0.5]; never more than one
   nag SMS in any 5-minute window (global); simultaneously-due items coalesce into a
   single SMS (numbered list + a short GPT "plan" line at the bottom).

**Scope: nags only.** The separate `Reminder` model (one-time/recurring clock reminders,
event pairs, basement-light flash) is left untouched. Gmail action items already create
nags, so they flow onto the today list for free. Exercise/briefing unchanged.

---

## Architecture

Two timing regimes coexist on top of the existing `NagSchedule` engine:

- **Clock-deadline items** (e.g. "timesheet by 11pm") — driven by the modified Zeno
  curve, escalating toward `deadline_at`.
- **Loose / daily items** (deadline = end of day) — driven by a new **context layer**:
  a randomized baseline so they nag a few times across the day, plus LLM-chosen
  surfacing when the user sends location/intent context.

Outbound nags pass through a new **global gate + coalescer** in `fire_due_nags`, so the
5-min floor and batching apply to both regimes uniformly.

New persistent state lives in the existing `app_state` key/value table (no new tables):
- `last_nag_sent_at` — UTC ISO of the last outbound nag SMS (global 5-min gate).
- `user_context` — JSON `{"text": "...", "at": "<UTC ISO>"}` of the latest location/intent.

---

## Changes by area

### 1. `.. ` capture prefix — `app/main.py`
In `/sms`, add a prefix check alongside the existing `#help`/`#newlist`/`##` shortcuts
(see `main.py:147-264`), but route it **through the intent pipeline** rather than handling
inline, so GPT can parse the deadline:
- If `Body` starts with `.. ` (dot-dot-space): strip the prefix, force `intent="create_nag"`
  by calling `parse_user_sms` with the remainder and a hint, then `handle_intent`.
- If GPT returns no `deadline_at`/`deadline_offset_minutes`, create the nag (defaults to
  11pm today via `_end_of_day_local`, already implemented at `intent_router.py:828`) **and**
  send a follow-up "When's the deadline? (reply a time, or 'none' for end of day)". Store a
  `PendingConfirmation` with `action_type="set_deadline"` and the new nag id in `payload`.
- Handle the deadline reply: in `/sms`, the pending-confirmation block (`main.py:266-330`)
  gains a `set_deadline` branch → parse the reply time and update `nag.deadline_at`
  (or leave the 11pm default if "none").

### 2. Context-update intent — `app/openai_client.py`, `app/intent_router.py`
- Add `"context_update"` to the intent list in the `parse_user_sms` system prompt
  (`openai_client.py:51`) with a section: trigger = a plain statement of where the user is
  or is heading / what they're doing ("at the office", "heading to Target", "home for the
  night"); data = `{"text": <verbatim>}`. Make it the classification for plain SMS that
  isn't another intent.
- Add `_handle_context_update(db, data)` in `intent_router.py` and register it in the
  `handlers` dict (`intent_router.py:189`): persist `user_context` to `app_state`, then call
  the new `evaluate_context(db)` (below) and return a brief ack (e.g. "Got it — near Target.").

### 3. Context-aware surfacing — new `app/context_engine.py` (+ called from scheduler)
- `evaluate_context(db)`: load open today-list items (active nags with a cycle today / loose
  deadline today), the stored `user_context`, and current local time. Ask GPT which items
  are worth surfacing *now* given (time of day, context text, each item's label/type). For
  each chosen item, pull `next_nag_at` forward to `now` so the normal gate/coalescer in
  `fire_due_nags` sends it (subject to the 5-min floor). New helper
  `select_relevant_items(items, context_text, local_now)` in `openai_client.py`.
- Called (a) immediately from `_handle_context_update`, and (b) once per scheduler tick
  guarded by a short interval (e.g. every ~10 min) so loose items still surface without context.
- **Baseline randomization**: when a loose/daily item's cycle starts, set its first
  `next_nag_at` to a random daytime slot (reuse `_random_nag_time`, `intent_router.py:35`)
  instead of firing immediately, so daily items nag "somewhat randomly through the day."

### 4. Zeno curve: random fraction — `app/scheduler.py:124`
In `_compute_deadline_interval`, replace the fixed `/3.0` with a per-call random fraction:
`fraction = random.uniform(0.25, 0.5); interval = remaining_minutes * fraction`. Keep the
`max(min_iv, ...)` clamp. (`random` import added.)

### 5. Global 5-min gate + coalescing — `app/scheduler.py:140` (`fire_due_nags`)
Rework the send portion (`scheduler.py:181-206`) so it no longer sends one SMS per nag:
1. Build the due set (status active, `next_nag_at <= now`), applying the existing
   per-nag cycle-start logic (`scheduler.py:150-166`) and quiet-hours deferral
   (`scheduler.py:168-179`) unchanged.
2. **Global gate**: read `last_nag_sent_at` from `app_state`; if
   `now - last_nag_sent_at < GLOBAL_NAG_MIN_GAP` (new config, default 5 min), skip sending
   this tick (items stay due, picked up next tick).
3. If sending: take the due items.
   - **One item** → existing rich message (`generate_deadline_nag_message`, `openai_client.py:168`).
   - **Multiple** → numbered list ("Due now:\n1) …\n2) …") + a short GPT "plan" line at the
     bottom via a new `generate_nag_plan(items)` in `openai_client.py` (graceful fallback to
     no plan line on GPT error). One `send_sms` call total.
4. After sending: set `last_nag_sent_at = now`; for each included nag advance `nag_count`
   and recompute `next_nag_at` via the modified interval; `_log_outbound` once.

New config in `app/config.py` (env + default, following the existing pattern at
`config.py:42-68`): `GLOBAL_NAG_MIN_GAP` (minutes, default 5).

### 6. Check-off "<thing> done" — reuse existing flow
No new code needed for matching: a plain SMS like "timesheet done" already parses to
`acknowledge` and runs `_keyword_prefilter` → GPT fallback → `execute_acknowledge`
(`intent_router.py:546-596`). For recurring daily items this already resets to the next
cron cycle (tomorrow) via `_next_nag_cycle` (`intent_router.py:80`); for one-offs it sets
`status="deleted"`. Verify the acknowledge prompt/keywords reliably catch the new phrasing;
extend `_ACK_STOP_WORDS` if needed.

### 7. Today-list UI — `app/ui.py`
Repurpose/extend the dashboard so `/` or `/today` shows the **nag today list**: active nags
whose cycle/deadline is today, with label, deadline, next nag, and a check-off button that
calls `execute_acknowledge`. Build on the existing `/nags` query (`ui.py:317-371`) and the
checklist rendering already in `ui.py`.

---

## Files touched
- `app/main.py` — `.. ` prefix routing + `set_deadline` pending branch.
- `app/openai_client.py` — `context_update` intent in prompt; `select_relevant_items`,
  `generate_nag_plan`.
- `app/intent_router.py` — `_handle_context_update`; minor ack-keyword tweak; baseline
  random scheduling for loose items at cycle start.
- `app/scheduler.py` — random Zeno fraction; global gate + coalescing rewrite of
  `fire_due_nags`; periodic `evaluate_context` call.
- `app/context_engine.py` — new: `evaluate_context`.
- `app/config.py` — `GLOBAL_NAG_MIN_GAP`.
- `app/ui.py` — today-list view.
- `CLAUDE.md` — document the new model.

No schema migrations (reuses `NagSchedule` + `app_state`). All new state is additive.

---

## Verification
1. **Unit-ish, locally**: import `_compute_deadline_interval` and assert the interval lands
   within `[remaining*0.25, remaining*0.5]` (clamped) across several calls.
2. **Capture flow**: POST a fake Twilio form to `/sms` with `Body=".. tell kids about
   mindfulness"` → expect a nag row created + a "When's the deadline?" SMS; reply "9pm" →
   `deadline_at` updates. Reply "none" on a second item → stays 11pm.
3. **Context flow**: POST `Body="heading to Target"` → `app_state.user_context` updated, ack
   returned, and any store/errand-type item gets `next_nag_at` pulled to now.
4. **Coalescing + gate**: create two nags with `deadline_at` ~now; run `fire_due_nags`
   twice within 5 min → exactly one combined SMS (numbered list + plan line), second tick
   sends nothing; after 5 min the next is allowed.
5. **Check-off**: `Body="mindfulness done"` → recurring item reschedules to tomorrow (no more
   nags today); one-off → `status="deleted"`.
6. **End-to-end**: `docker-compose up`, watch scheduler logs for the gate/coalesce log lines
   and the `/today` UI reflecting the list. (Twilio sends can be stubbed by pointing
   `send_sms` at a no-op / checking `sms_log`.)
