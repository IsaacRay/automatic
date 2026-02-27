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
- "intent": one of "create_reminder", "create_recurring", "acknowledge", "cancel", "snooze", "list", "help", "unknown"
- "data": intent-specific fields (see below)

Intent-specific data:

**create_reminder**: The user wants to be reminded about something at a specific time.
- "label": short description of the event/reminder
- "reminders": array of objects, each with:
  - "message": the SMS text to send
  - "fire_at": ISO 8601 datetime string in UTC (convert from {USER_TIMEZONE})
For meetings/events: create TWO reminders — a prep reminder 30 minutes before AND the event reminder. Use parent_event_id to link them.
  - The 30-minute prep reminder message should say "Heads up — [event] at [event time, e.g. 2:00 PM]" so the user knows what's coming and when.
  - The event-time reminder message should say "Time for [event]" or similar.
- "parent_event_id": a unique string to group related reminders (use format "evt_<timestamp>")

**create_recurring**: The user wants repeated messages on a schedule.
- "label": short description
- "cron_expression": standard 5-field cron expression (minute hour day month weekday)
- "message_prompt": a prompt to send to an AI each firing to generate a fresh, varied message. Should capture the spirit/purpose of what the user wants.

**acknowledge**: The user is marking something as done. Trigger words: "done", "finished", "completed", "got it", "handled".
- "keyword": optional keyword to match a specific item (null to mark most recent)
- "all": boolean, true if the user says "done all" or "clear all"

**cancel**: The user wants to cancel/delete a reminder, recurring schedule, or action item. Trigger words: "cancel", "delete", "remove", "nevermind", "nvm", "forget it", "stop", "kill".
Use this intent when the user wants to get rid of something they no longer need — different from "acknowledge" which means they completed the task.
- "keyword": optional keyword to match a specific item (null to cancel most recent pending reminder)
- "type": optional — "reminder", "recurring", or "action" to narrow scope. If not specified, searches all types.

**snooze**: The user wants to delay reminders. Trigger words: "snooze", "later", "not now", "remind me later".
- "duration_minutes": how long to snooze (default 60)
- "keyword": optional keyword to match a specific item

**list**: The user wants to see their pending items. Trigger words: "list", "show", "what do I have", "status", "pending".
No additional data needed.

**help**: The user is asking what they can do or how the bot works.
No additional data needed.

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


def generate_recurring_message(prompt: str) -> str:
    """Generate a fresh motivational/reminder message for a recurring schedule.

    Called by the scheduler each time a recurring schedule fires.
    """
    return _chat(
        [
            {
                "role": "system",
                "content": "Generate a short, encouraging SMS message (under 160 characters). "
                "Be varied and creative — never repeat the same phrasing. "
                "Keep it warm, direct, and actionable.",
            },
            {"role": "user", "content": prompt},
        ],
        temperature=0.9,
    )


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
                '- "description": concise action item text\n'
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
