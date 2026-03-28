"""OpenAI API calls — raw HTTP matching existing pattern."""

import json
import urllib.request
from datetime import datetime

from app.config import OPENAI_API_KEY, USER_TIMEZONE


def _chat(messages: list, *, temperature: float = 0.3, json_mode: bool = False) -> str:
    """Make a chat completion request to OpenAI."""
    payload: dict = {
        "model": "gpt-4o",
        "messages": messages,
        "temperature": temperature,
    }
    if json_mode:
        payload["response_format"] = {"type": "json_object"}

    req = urllib.request.Request(
        "https://api.openai.com/v1/chat/completions",
        data=json.dumps(payload).encode(),
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {OPENAI_API_KEY}",
        },
    )
    with urllib.request.urlopen(req) as resp:
        result = json.loads(resp.read())
    return result["choices"][0]["message"]["content"]


def parse_user_sms(message: str) -> dict:
    """Parse a user SMS into a structured intent via GPT-4o.

    Returns a dict with 'intent' and 'data' keys.
    """
    now = datetime.now()
    # Import here to get timezone-aware time
    try:
        from zoneinfo import ZoneInfo
        now_local = datetime.now(ZoneInfo(USER_TIMEZONE))
    except Exception:
        now_local = now

    system_prompt = f"""You are an SMS assistant for someone with ADHD. Parse the user's text message and return structured JSON.

Current date/time: {now_local.strftime("%A, %B %d, %Y %I:%M %p")} ({USER_TIMEZONE})

Return a JSON object with:
- "intent": one of "create_reminder", "create_nag", "reschedule", "acknowledge", "cancel", "snooze", "list", "briefing", "help", "log_exercise", "exercise_history", "unknown"
- "data": intent-specific fields (see below)

Intent-specific data:

**create_reminder**: The user wants to be reminded about something — either once at a specific time, or on a recurring schedule.
- "label": short description of the event/reminder
- "message": the SMS text to send (e.g., "Reminder: Dr Watson appointment")
- "reminders": array of objects, each with:
  - "message": the SMS text to send
  - "fire_at": ISO 8601 datetime string in {USER_TIMEZONE} local time (do NOT convert to UTC)
For meetings/events: create TWO reminders — a prep reminder 30 minutes before AND the event reminder. Use parent_event_id to link them.
  - The 30-minute prep reminder message should say "Heads up — [event] at [event time, e.g. 2:00 PM]" so the user knows what's coming and when.
  - The event-time reminder message should say "Time for [event]" or similar.
- "parent_event_id": a unique string to group related reminders (use format "evt_<timestamp>")
- "cron_expression": optional, 5-field cron expression for recurring reminders (minute hour day month weekday). Set this when the user wants to be reminded on a recurring schedule (e.g., "every Tuesday at 3pm" → "0 15 * * 2", "every day at 5pm" → "0 17 * * *", "every weekday at 9am" → "0 9 * * 1-5"). Do NOT set this for one-time reminders. When cron_expression is set, do NOT use parent_event_id or create prep/event pairs — just provide a single "message" field with the reminder text.

**create_nag**: The user wants to be nagged repeatedly at a fixed interval until they reply "done". Trigger words: "nag", "keep reminding", "bug me", "pester", "every X minutes until", "nag me".
This is different from a recurring reminder (which fires once per schedule) — nags repeat rapidly within a window until acknowledged.

IMPORTANT — distinguish TWO separate concepts:
  (a) INTERVAL: how often to nag within a single cycle (e.g. "every 15 min" → interval_minutes=15). This is the rapid nagging frequency.
  (b) RECURRENCE: how often the nag cycle repeats (e.g. "weekdays", "daily", "weekly"). This sets repeating=true and controls the cron_expression.
A message like "nag me every 15 min weekdays at 9am" has BOTH: interval=15, recurrence=weekdays at 9am.
A message like "nag me every 30 min to call the dentist" has ONLY interval=30, no recurrence (one-time nag).

- "label": short description (e.g. "enter timesheet")
- "message": the nag SMS text to send each time (e.g. "Hey! Enter your timesheet!")
- "cron_expression": 5-field cron for when each nag CYCLE starts (e.g. "0 9 * * 1-5" for 9am weekdays). For completion-anchored nags, this controls the nag time-of-day only (e.g. "0 9 * * *" for 9am daily nags). If the user doesn't specify a time, use "0 12 * * *" as a placeholder — the system will pick a random time between 9am-5pm automatically.
  Common recurrence-to-cron mappings:
    - "daily at 9am" → "0 9 * * *"
    - "weekdays at 9am" → "0 9 * * 1-5"
    - "weekly on Monday at 9am" → "0 9 * * 1"
    - "monthly on the 1st at 9am" → "0 9 1 * *"
- "interval_minutes": how often to nag within the cycle (e.g. 15). This is the rapid nagging frequency within a window, NOT the recurrence.
- "max_duration_minutes": how long the nag window lasts from cycle start (e.g. 120 for 2 hours). null if no limit specified by the user.
- "user_specified_time": boolean. true if the user explicitly mentioned a time (e.g. "at 9am", "at noon", "in 2 hours"). false if no time was given (e.g. "nag me to call the dentist"). When false, the system picks a random time between 9am-5pm.
- "repeating": boolean (default false). If false, the nag fires for ONE cycle only and is deleted once the user replies DONE or the window expires. If true, it repeats on the cron schedule. Set to true when recurrence language is present (e.g. "daily", "weekdays", "every Monday", "every month"). Keep false for one-shot nags with no recurrence (e.g. "nag me to call the dentist every 30 min").
- "recurrence_description": string or null. Human-readable description of the recurrence pattern. Examples: "daily at 9:00 AM", "weekdays at 9:00 AM", "weekly on Monday at 9:00 AM", "monthly on the 1st at 9:00 AM". Set to null when repeating=false.
- "anchor_to_completion": boolean (default false). If true, the NEXT cycle starts relative to when the user completes the task, not the cron schedule. Implies repeating=true. Example: "give dog meds every month, nag daily until done" — if due March 22 but completed March 24, next cycle starts April 24. Use this when the user says things like "adjust the next date based on when I finish" or implies a monthly/weekly task where timing shifts based on completion.
- "cycle_months": integer or null. Number of months between cycles when anchor_to_completion is true (e.g. 1 for monthly).
- "cycle_days": integer or null. Number of days between cycles when anchor_to_completion is true. Use this OR cycle_months, not both. For "every 2 weeks" use 14.
- "first_nag_at": ISO 8601 datetime in {USER_TIMEZONE} local time (do NOT convert to UTC) for when the FIRST cycle should start, if the user specifies a specific date (e.g. "starting March 22nd"). null if not specified (will use cron_expression to compute).

**acknowledge**: The user is marking something as done. Trigger words: "done", "finished", "completed", "got it", "handled".
- "keyword": optional keyword to match a specific item (null to mark most recent)
- "all": boolean, true if the user says "done all" or "clear all"

**cancel**: The user wants to cancel/delete a reminder, recurring schedule, nag, or action item. Trigger words: "cancel", "delete", "remove", "nevermind", "nvm", "forget it", "stop", "kill".
Use this intent when the user wants to get rid of something they no longer need — different from "acknowledge" which means they completed the task.
- "keyword": optional keyword to match a specific item (null to cancel most recent pending reminder)
- "type": optional — "reminder", "recurring", "nag", or "action" to narrow scope. If not specified, searches all types. Only set this if you're very confident about the type — when in doubt, leave it null to search everything.

**reschedule**: The user wants to move an existing reminder/event to a new time. Trigger words: "reschedule", "move", "change to", "push to", "bump to", "actually make it".
- "keyword": keyword to match the existing reminder/event
- "new_time": ISO 8601 datetime string in {USER_TIMEZONE} local time (do NOT convert to UTC)
- "original_message": the user's original message text verbatim (needed for fuzzy matching)

**snooze**: The user wants to delay reminders. Trigger words: "snooze", "later", "not now", "remind me later".
- "duration_minutes": how long to snooze (default 60)
- "keyword": optional keyword to match a specific item

**list**: The user wants to see their pending items. Trigger words: "list", "show", "what do I have", "status", "pending".
No additional data needed.

**help**: The user is asking what they can do or how the bot works. Trigger words: "commands", "info", "what can you do", "how does this work", "options", "menu".
NOTE: The word "help" alone is reserved by the carrier and won't reach us — use the triggers above instead.
No additional data needed.

**briefing**: The user wants their morning briefing (weather, calendar, market summary). Trigger words: "briefing", "morning briefing", "brief me", "what's my day look like", "daily briefing", "today's briefing".
No additional data needed.

**log_exercise**: The user reports completing an exercise activity. Trigger words/patterns: "I ran", "I biked", "went for a run", "did a bike ride", "rode the bike", "indoor bike", "exercised", "I walked", "went for a walk".
- "activity": string — "run", "outdoor bike", "indoor bike", "walk", etc.
- "duration_minutes": integer or null
- "distance_miles": float or null
- "notes": string or null — any extra context

**exercise_history**: The user asks about past exercise activities. Trigger words: "exercise history", "what exercise", "my activities", "workouts between", "how much did I exercise", "my runs", "my workouts".
- "start_date": ISO date string (YYYY-MM-DD) — convert relative dates like "last week" or "this week" using current date
- "end_date": ISO date string (YYYY-MM-DD)

**unknown**: You can't determine the intent.
- "original": the original message text

Be generous in interpretation — this is for someone with ADHD who texts casually. "mtg at 4 fri esub lambdas" means "I have a meeting at 4pm this Friday about Esub Lambdas"."""

    content = _chat(
        [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": message},
        ],
        temperature=0.3,
        json_mode=True,
    )
    return json.loads(content)


def deduce_reschedule_target(user_message: str, items: list[dict], *, parsed_new_time: str = "") -> dict:
    """Use GPT-4o to fuzzy-match a reschedule request against pending items.

    Args:
        user_message: The user's raw SMS text.
        items: List of dicts with keys: id, type ("reminder"|"recurring"),
               label, fire_at (ISO string).
        parsed_new_time: Optional ISO 8601 UTC time already parsed from the user's
                         message by the intent parser. Use as a strong hint.

    Returns a dict with matched_id, matched_type, new_time, description
    or matched_id=None if no match.
    """
    items_text = json.dumps(items, indent=2)

    try:
        from zoneinfo import ZoneInfo
        now_local = datetime.now(ZoneInfo(USER_TIMEZONE))
    except Exception:
        now_local = datetime.now()

    time_hint = ""
    if parsed_new_time:
        time_hint = f"\nThe user's intended new time has already been parsed as: {parsed_new_time}. Use this as the new_time unless it seems clearly wrong.\n"

    system_prompt = f"""You are helping match a reschedule request to the correct item. The user has ADHD and texts very casually — expect abbreviations, typos, partial words, and terse messages.

Current date/time: {now_local.strftime("%A, %B %d, %Y %I:%M %p")} ({USER_TIMEZONE})

The user sent this message wanting to reschedule something:
"{user_message}"
{time_hint}
Here are their pending items:
{items_text}

MATCHING STRATEGY — try ALL of these, pick the best overall match:
1. Substring/keyword: does ANY word in the user's message appear in the item's label? (e.g., "move dentist" matches "dentist appointment")
2. Semantic/synonym: does the user's meaning match? (e.g., "push the doc appt" matches "dentist appointment", "move standup" matches "daily standup meeting")
3. Time-based: does the user reference a time that matches an item's fire_at? (e.g., "move the 3pm thing" matches item firing at 3:00 PM)
4. Abbreviation/shorthand: expand common abbreviations (e.g., "mtg"=meeting, "appt"=appointment, "dr"=doctor, "dent"=dentist, "esub"=Esub)
5. Fuzzy/typo: allow off-by-one typos and phonetic similarity (e.g., "meating" matches "meeting")

PRIORITY — match quality is king, item type/status is a tiebreaker:
1. BEST keyword overlap wins — if the user's words appear literally in one item's label but not another's, pick that item regardless of type.
2. More keyword overlap > less overlap.
3. Exact substring > semantic similarity — a word literally appearing in a label beats a loosely related concept.
4. Only use item type/status or time proximity as a tiebreaker when keyword match quality is equal.
If only ONE item exists, match it unless the user's description actively contradicts it.

Return a JSON object with:
- "matched_id": the integer ID of the matched item (as an integer, not a string), or null if no reasonable match
- "matched_type": "reminder" or "recurring"
- "new_time": ISO 8601 datetime string in {USER_TIMEZONE} local time (do NOT convert to UTC)
- "description": a short human-readable summary like "Dentist appointment → Wed Mar 5 3:00 PM"

If you cannot determine a match, return {{"matched_id": null}}.
Err on the side of matching — a false match can be rejected by the user via confirmation, but a false null means they have to retype."""

    content = _chat(
        [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_message},
        ],
        temperature=0.3,
        json_mode=True,
    )
    return json.loads(content)


def deduce_acknowledge_target(user_message: str, items: list[dict]) -> dict:
    """Use GPT-4o to fuzzy-match an acknowledge/done request against pending items.

    Args:
        user_message: The user's raw SMS text.
        items: List of dicts with keys: id, type, label, detail.

    Returns a dict with matched_id, matched_type
    or matched_id=None if no match.
    """
    items_text = json.dumps(items, indent=2)

    try:
        from zoneinfo import ZoneInfo
        now_local = datetime.now(ZoneInfo(USER_TIMEZONE))
    except Exception:
        now_local = datetime.now()

    system_prompt = f"""You are helping match a "done" / acknowledgment request to the correct item. The user has ADHD and texts very casually — expect abbreviations, typos, partial words, and terse messages.

Current date/time: {now_local.strftime("%A, %B %d, %Y %I:%M %p")} ({USER_TIMEZONE})

The user sent this message marking something as done or completed:
"{user_message}"

Here are their pending items:
{items_text}

Each item has: id, type, label, detail (schedule/time info), and often a "message" field with the actual reminder text.

MATCHING STRATEGY — try ALL of these across ALL fields (label, message, detail), pick the best overall match:
1. Substring/keyword: does ANY word in the user's message appear in any field? (e.g., "done dentist" matches label "dentist appointment" OR message "Time for dentist")
2. Semantic/synonym: does the user's meaning match? (e.g., "finished the teeth thing" matches "dentist appointment", "done with meds" matches "take medication")
3. Type-based: does the user reference a type? (e.g., "finished the nag" → prefer nag-type items, "done with the reminder" → prefer reminder-type)
4. Time-based: does the user reference a time matching the detail? (e.g., "done with the 3pm" matches item with "fires 3:00 PM" in detail)
5. Abbreviation/shorthand: expand common abbreviations (e.g., "ts"=timesheet, "mtg"=meeting, "appt"=appointment, "dr"=doctor, "dent"=dentist, "meds"=medication/medicine)
6. Fuzzy/typo: allow off-by-one typos and phonetic similarity (e.g., "timesheat" matches "timesheet")

PRIORITY — match quality is king, item type/status is a tiebreaker:
1. BEST keyword overlap wins — if the user's words appear literally in one item's label but not another's, pick that item regardless of type. Example: "replace tire done" must match "Call to make appointment to replace tire and fix window" over "make an appointment for car window repair" because "replace tire" appears in the first label.
2. More keyword overlap > less overlap — count how many of the user's words appear in each item's label/message. Pick the item with the most hits.
3. Exact substring > semantic similarity — "tire" literally appearing in a label beats "car-related" semantic association.
4. Only use item type/status as a tiebreaker when keyword match quality is equal.
- If only ONE item exists, match it unless the user's message actively contradicts it.

Return a JSON object with:
- "matched_id": the integer ID of the matched item (as an integer, not a string), or null if no reasonable match
- "matched_type": the "type" field of the matched item

If you cannot determine a match, return {{"matched_id": null}}.
Err on the side of matching — a false match can be rejected by the user via confirmation, but a false null means they have to retype."""

    content = _chat(
        [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_message},
        ],
        temperature=0.3,
        json_mode=True,
    )
    return json.loads(content)


def deduce_cancel_target(user_message: str, items: list[dict]) -> dict:
    """Use GPT-4o to fuzzy-match a cancel request against pending items.

    Args:
        user_message: The user's raw SMS text.
        items: List of dicts with keys: id, type, label, detail.

    Returns a dict with matched_id, matched_type
    or matched_id=None if no match.
    """
    items_text = json.dumps(items, indent=2)

    try:
        from zoneinfo import ZoneInfo
        now_local = datetime.now(ZoneInfo(USER_TIMEZONE))
    except Exception:
        now_local = datetime.now()

    system_prompt = f"""You are helping match a cancel/delete request to the correct item. The user has ADHD and texts very casually — expect abbreviations, typos, partial words, and terse messages.

Current date/time: {now_local.strftime("%A, %B %d, %Y %I:%M %p")} ({USER_TIMEZONE})

The user sent this message wanting to cancel something:
"{user_message}"

Here are their pending items:
{items_text}

Each item has: id, type, label, detail (schedule/time info), and often a "message" field with the actual reminder text.

MATCHING STRATEGY — try ALL of these across ALL fields (label, message, detail), pick the best overall match:
1. Substring/keyword: does ANY word in the user's message appear in any field? (e.g., "cancel dentist" matches label "dentist appointment" OR message "Time for dentist")
2. Semantic/synonym: does the user's meaning match? (e.g., "nvm the teeth thing" matches "dentist appointment", "kill the meds nag" matches "take medication")
3. Type-based: does the user reference a type? (e.g., "stop the nag" → prefer nag-type items, "cancel the reminder" → prefer reminder-type, "stop the recurring" → prefer recurring-type)
4. Time-based: does the user reference a time matching the detail? (e.g., "cancel the 3pm thing" matches item with "fires 3:00 PM" in detail)
5. Abbreviation/shorthand: expand common abbreviations (e.g., "ts"=timesheet, "mtg"=meeting, "appt"=appointment, "dr"=doctor, "dent"=dentist, "meds"=medication/medicine)
6. Fuzzy/typo: allow off-by-one typos and phonetic similarity (e.g., "cancl meating" matches "meeting")

Strip away cancel-intent words before matching keywords: ignore "cancel", "delete", "remove", "nvm", "nevermind", "forget", "stop", "kill", "drop", "get rid of", "the", "my", "that" — focus on the REMAINING words as the search terms.

PRIORITY — match quality is king, item type/status is a tiebreaker:
1. BEST keyword overlap wins — if the user's words appear literally in one item's label but not another's, pick that item regardless of type.
2. More keyword overlap > less overlap — count how many of the user's words appear in each item's label/message. Pick the item with the most hits.
3. Exact substring > semantic similarity — a word literally appearing in a label beats a loosely related concept.
4. Only use item type/status as a tiebreaker when keyword match quality is equal.
- If only ONE item exists, match it unless the user's message actively contradicts it.

Return a JSON object with:
- "matched_id": the integer ID of the matched item (as an integer, not a string), or null if no reasonable match
- "matched_type": the "type" field of the matched item

If you cannot determine a match, return {{"matched_id": null}}.
Err on the side of matching — a false match can be rejected by the user via confirmation, but a false null means they have to retype."""

    content = _chat(
        [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_message},
        ],
        temperature=0.3,
        json_mode=True,
    )
    return json.loads(content)


def extract_action_items_structured(emails: list[dict]) -> list[dict]:
    """Extract structured action items from email bodies.

    Returns a list of dicts with 'description' and 'source_ref' keys.
    """
    email_text = ""
    for i, e in enumerate(emails, 1):
        email_text += f"--- Email {i} (Date: {e['date']}, Subject: {e['subject']}) ---\n"
        email_text += e["body"] + "\n\n"

    content = _chat(
        [
            {
                "role": "system",
                "content": "You are analyzing emails sent from Kathryn to Isaac. "
                "Extract individual action items — tasks, replies needed, decisions, favors. "
                "Return a JSON object with an 'items' array. Each item has:\n"
                '- "description": short, actionable text suitable as an SMS reminder (e.g. "Call dentist to reschedule", "Reply to Kathryn about dinner plans"). '
                "Do NOT include email metadata, dates, subjects, or sender info in the description — just the task itself.\n"
                '- "source_ref": "Email: <subject> (<date>)"\n\n'
                "If no action items exist, return {\"items\": []}.",
            },
            {
                "role": "user",
                "content": f"Extract action items from these emails:\n\n{email_text}",
            },
        ],
        temperature=0.3,
        json_mode=True,
    )
    result = json.loads(content)
    return result.get("items", [])
