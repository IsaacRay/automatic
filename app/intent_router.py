"""Map parsed intents to DB operations and reply text."""

import random
import uuid
from datetime import datetime, timedelta, timezone

from sqlalchemy.orm import Session

from app.models import Reminder, PendingConfirmation, NagSchedule, ExerciseLog
from app.config import USER_PHONE, USER_TIMEZONE


def _parse_dt(s: str) -> datetime:
    """Parse an ISO 8601 datetime string, treating naive strings as local time."""
    from zoneinfo import ZoneInfo
    dt = datetime.fromisoformat(s)
    if dt.tzinfo is None:
        local_tz = ZoneInfo(USER_TIMEZONE)
        dt = dt.replace(tzinfo=local_tz)
    return dt.astimezone(timezone.utc)


def _now_local():
    from zoneinfo import ZoneInfo
    return datetime.now(ZoneInfo(USER_TIMEZONE))


def _format_time(dt: datetime) -> str:
    """Format a UTC datetime into a human-readable local time string."""
    from zoneinfo import ZoneInfo
    local = dt.astimezone(ZoneInfo(USER_TIMEZONE))
    return local.strftime("%a %b %d %I:%M %p")


def _random_nag_time() -> datetime:
    """Pick a random time between 9am–5pm today (or tomorrow if past 5pm) in the user's timezone, returned as UTC."""
    from zoneinfo import ZoneInfo
    tz = ZoneInfo(USER_TIMEZONE)
    local_now = datetime.now(tz)
    # Random hour 9–16, random minute 0–59
    hour = random.randint(9, 16)
    minute = random.randint(0, 59)
    candidate = local_now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if candidate <= local_now:
        candidate += timedelta(days=1)
    return candidate.astimezone(timezone.utc)


def _next_cron_fire(cron_expr: str, tz_name: str) -> datetime:
    """Compute the next fire time for a cron expression, returned as UTC."""
    from zoneinfo import ZoneInfo
    from croniter import croniter
    local_now = datetime.now(ZoneInfo(tz_name))
    cron = croniter(cron_expr, local_now)
    next_local = cron.get_next(datetime)
    return next_local.astimezone(timezone.utc)


def _next_nag_cycle(nag, completion_time: datetime = None) -> datetime:
    """Compute the next cycle start for a nag schedule.

    If anchor_to_completion is True, the next cycle is relative to completion_time.
    Otherwise, falls back to the cron expression.
    """
    if nag.anchor_to_completion and completion_time:
        from zoneinfo import ZoneInfo
        from dateutil.relativedelta import relativedelta
        local_completion = completion_time.astimezone(ZoneInfo(nag.timezone))
        if nag.cycle_months:
            next_local = local_completion + relativedelta(months=nag.cycle_months)
        elif nag.cycle_days:
            next_local = local_completion + timedelta(days=nag.cycle_days)
        else:
            return _next_cron_fire(nag.cron_expression, nag.timezone)
        # Preserve the nag start hour/minute from the cron expression
        from croniter import croniter
        # Parse hour/minute from cron (fields: min hour dom month dow)
        parts = nag.cron_expression.split()
        if len(parts) >= 2:
            try:
                cron_minute = int(parts[0])
                cron_hour = int(parts[1])
                next_local = next_local.replace(hour=cron_hour, minute=cron_minute, second=0, microsecond=0)
            except ValueError:
                pass  # wildcard or complex cron — just keep the completion time-of-day
        return next_local.astimezone(timezone.utc)
    return _next_cron_fire(nag.cron_expression, nag.timezone)



_ACK_STOP_WORDS = frozenset({
    "done", "finished", "completed", "got", "handled", "did", "do",
    "with", "the", "my", "a", "an", "is", "it", "i", "for", "to",
})

_CANCEL_STOP_WORDS = frozenset({
    "cancel", "delete", "remove", "nvm", "nevermind", "forget", "stop",
    "kill", "drop", "the", "my", "a", "an", "is", "it", "i", "for", "to",
    "get", "rid", "of", "that", "about",
})

_SNOOZE_STOP_WORDS = frozenset({
    "snooze", "later", "not", "now", "remind", "me", "delay", "pause",
    "the", "my", "a", "an", "is", "it", "i", "for", "to", "that", "about",
})


def _parse_snooze_duration(text: str) -> int:
    """Parse a snooze duration from raw message text. Returns minutes, default 60."""
    import re
    t = text.lower()
    # "1440 minutes", "30 min", "2 hours", "1 hour", "1 day", etc.
    m = re.search(r'(\d+(?:\.\d+)?)\s*(min(?:ute)?s?|hrs?|hours?|days?)', t)
    if m:
        val = float(m.group(1))
        unit = m.group(2)
        if unit.startswith("h"):
            return int(val * 60)
        elif unit.startswith("d"):
            return int(val * 1440)
        else:
            return int(val)
    # "half hour", "half a day" — check before "a day"/"an hour"
    if re.search(r'\bhalf\s+(an?\s+)?hour\b', t):
        return 30
    if re.search(r'\bhalf\s+(a\s+)?day\b', t):
        return 720
    # "a day", "a hour"
    if re.search(r'\ba\s+day\b', t):
        return 1440
    if re.search(r'\ban?\s+hour\b', t):
        return 60
    return 60


def _keyword_prefilter(search_text: str, items: list[dict], stop_words: frozenset) -> dict | None:
    """Match items by keyword overlap in label/message before resorting to GPT.

    Returns the best item if one clearly wins, otherwise None (fall back to GPT).
    """
    words = [w.lower() for w in search_text.split() if w.lower() not in stop_words and len(w) > 1]
    if not words:
        return None

    scores = []
    for item in items:
        searchable = f"{item.get('label', '')} {item.get('message', '')}".lower()
        hits = sum(1 for w in words if w in searchable)
        scores.append((hits, item))

    scores.sort(key=lambda x: x[0], reverse=True)

    if scores[0][0] == 0:
        return None  # no keyword hits at all

    # Clear winner: top match has strictly more hits than runner-up
    if len(scores) == 1 or scores[0][0] > scores[1][0]:
        return scores[0][1]

    return None  # ambiguous — let GPT decide


def handle_intent(db: Session, parsed: dict) -> str:
    """Dispatch a parsed intent to the appropriate handler. Returns reply text."""
    intent = parsed.get("intent", "unknown")
    data = parsed.get("data", {})

    handlers = {
        "create_reminder": _handle_create_reminder,
        "create_nag": _handle_create_nag,
        "reschedule": _handle_reschedule,
        "acknowledge": _handle_acknowledge,
        "cancel": _handle_cancel,
        "snooze": _handle_snooze,
        "list": _handle_list,
        "briefing": _handle_briefing,
        "help": _handle_help,
        "log_exercise": _handle_log_exercise,
        "exercise_history": _handle_exercise_history,
    }
    handler = handlers.get(intent)
    if handler:
        return handler(db, data)
    return "I didn't understand that. Text COMMANDS to see what I can do."


def _handle_create_reminder(db: Session, data: dict) -> str:
    label = data.get("label", "Reminder")
    reminders_data = data.get("reminders", [])
    parent_event_id = data.get("parent_event_id") or f"evt_{uuid.uuid4().hex[:12]}"
    cron_expr = data.get("cron_expression")

    if not reminders_data and not cron_expr:
        return "I couldn't figure out when to remind you. Try again with a time?"

    # Recurring reminder (has cron_expression)
    if cron_expr:
        message = data.get("message") or f"Reminder: {label}"
        if reminders_data:
            fire_at = _parse_dt(reminders_data[0]["fire_at"])
            message = reminders_data[0].get("message", message)
        else:
            fire_at = _next_cron_fire(cron_expr, USER_TIMEZONE)

        reminder = Reminder(
            user_phone=USER_PHONE,
            label=label,
            fire_at=fire_at,
            message=message,
            cron_expression=cron_expr,
            timezone=USER_TIMEZONE,
            status="pending",
        )
        db.add(reminder)
        db.commit()
        return f"Recurring reminder set: \"{label}\" ({cron_expr}). Next: {_format_time(fire_at)}"

    # One-shot reminder(s)
    created = []
    for r in reminders_data:
        fire_at = _parse_dt(r["fire_at"])
        reminder = Reminder(
            user_phone=USER_PHONE,
            label=label,
            fire_at=fire_at,
            message=r["message"],
            parent_event_id=parent_event_id,
            status="pending",
        )
        db.add(reminder)
        created.append(fire_at)

    db.commit()

    times = ", ".join(_format_time(t) for t in sorted(created))
    count = len(created)
    noun = "reminder" if count == 1 else "reminders"
    return f"Got it! Set {count} {noun} for \"{label}\" at: {times}"


def _handle_reschedule(db: Session, data: dict) -> str:
    import json as _json
    import logging
    from app.openai_client import deduce_reschedule_target

    log = logging.getLogger(__name__)

    # Use raw message body as fallback — GPT doesn't always echo original_message
    original_message = (
        data.get("original_message")
        or data.get("_raw_message")
        or data.get("keyword", "")
    )
    # The initial parse may have already extracted the new time — pass as hint
    parsed_new_time = data.get("new_time", "")

    # Gather all pending reminders (includes recurring ones)
    reminders = db.query(Reminder).filter(
        Reminder.user_phone == USER_PHONE,
        Reminder.status.in_(["pending", "sent"]),
    ).order_by(Reminder.fire_at.asc()).all()

    # De-duplicate event pairs: for reminders sharing a parent_event_id,
    # only show the event-time (latest fire_at) entry to avoid confusing the matcher
    event_groups = {}  # parent_event_id -> reminder with latest fire_at
    standalone = []
    for r in reminders:
        if r.parent_event_id:
            existing = event_groups.get(r.parent_event_id)
            if not existing or r.fire_at > existing.fire_at:
                event_groups[r.parent_event_id] = r
        else:
            standalone.append(r)

    items = []
    for r in list(event_groups.values()) + standalone:
        rtype = "recurring" if r.cron_expression else "reminder"
        items.append({
            "id": r.id,
            "type": rtype,
            "label": r.label,
            "fire_at": r.fire_at.isoformat() if r.fire_at else None,
        })

    if not items:
        return "No pending reminders to reschedule!"

    # Ask GPT-4o to fuzzy-match, passing the already-parsed time as a hint
    result = deduce_reschedule_target(original_message, items, parsed_new_time=parsed_new_time)
    log.info("Reschedule match result: %s", result)

    if not result.get("matched_id"):
        return "Couldn't figure out what to reschedule. Text LIST to see your items."

    # Coerce matched_id to int — GPT sometimes returns it as a string
    try:
        matched_id = int(result["matched_id"])
    except (ValueError, TypeError):
        log.warning("Invalid matched_id from GPT: %s", result.get("matched_id"))
        return "Couldn't figure out what to reschedule. Text LIST to see your items."

    # Capture previous state for undo before executing
    payload = {
        "matched_id": matched_id,
        "matched_type": result["matched_type"],
        "new_time": result["new_time"],
        "description": result.get("description", ""),
    }
    undo_state = _capture_reschedule_undo(db, matched_id, result["matched_type"])

    # Execute immediately
    reply = execute_reschedule(db, payload)

    # Store undo confirmation
    undo_payload = {**payload, "undo_state": undo_state}
    db.query(PendingConfirmation).filter(
        PendingConfirmation.user_phone == USER_PHONE,
    ).delete()
    db.add(PendingConfirmation(
        user_phone=USER_PHONE,
        action_type="undo_reschedule",
        payload=_json.dumps(undo_payload),
    ))
    db.commit()

    return f"{reply}. Reply UNDO to reverse."


def execute_reschedule(db: Session, payload: dict) -> str:
    """Execute a confirmed reschedule action. Called after user replies YES."""
    import logging
    log = logging.getLogger(__name__)

    matched_id = payload["matched_id"]
    matched_type = payload["matched_type"]
    new_event_time = _parse_dt(payload["new_time"])
    new_prep_time = new_event_time - timedelta(minutes=30)

    event_time_str = _format_time(new_event_time)

    if matched_type in ("reminder", "recurring"):
        reminder = db.query(Reminder).filter(
            Reminder.id == matched_id,
            Reminder.status.in_(["pending", "sent"]),
        ).first()
        if not reminder:
            return "That reminder no longer exists or was already dismissed."

        label = reminder.label

        # If part of an event pair, reschedule all non-cancelled siblings
        if reminder.parent_event_id:
            siblings = db.query(Reminder).filter(
                Reminder.parent_event_id == reminder.parent_event_id,
                Reminder.status != "cancelled",
            ).order_by(Reminder.fire_at.asc()).all()

            if len(siblings) == 2:
                siblings[0].fire_at = new_prep_time
                siblings[0].status = "pending"
                siblings[0].message = f"Heads up \u2014 {label} at {event_time_str}"
                siblings[1].fire_at = new_event_time
                siblings[1].status = "pending"
                siblings[1].message = f"Time for {label}"
                log.info("Rescheduled event pair (ids %d, %d) to prep=%s event=%s",
                         siblings[0].id, siblings[1].id, new_prep_time, new_event_time)
            else:
                for s in siblings:
                    s.fire_at = new_event_time
                    s.status = "pending"
                    s.message = f"Time for {label}"
                log.info("Rescheduled %d sibling(s) for parent %s to %s",
                         len(siblings), reminder.parent_event_id, new_event_time)
        else:
            reminder.fire_at = new_event_time
            reminder.status = "pending"
            reminder.message = f"Reminder: {label} at {event_time_str}"
            log.info("Rescheduled reminder #%d to %s", reminder.id, new_event_time)

        db.commit()
        time_str = _format_time(new_event_time)
        prefix = "Rescheduled recurring" if reminder.cron_expression else "Rescheduled"
        return f"{prefix} \"{reminder.label}\" to {time_str}"

    return "Unknown item type."


def _capture_reschedule_undo(db: Session, matched_id: int, matched_type: str) -> list[dict]:
    """Capture the current state of reminder(s) before a reschedule, for undo."""
    snapshots = []
    if matched_type in ("reminder", "recurring"):
        reminder = db.query(Reminder).filter(
            Reminder.id == matched_id,
            Reminder.status.in_(["pending", "sent"]),
        ).first()
        if not reminder:
            return snapshots
        if reminder.parent_event_id:
            siblings = db.query(Reminder).filter(
                Reminder.parent_event_id == reminder.parent_event_id,
                Reminder.status != "cancelled",
            ).all()
            for s in siblings:
                snapshots.append({
                    "id": s.id, "fire_at": s.fire_at.isoformat(),
                    "status": s.status, "message": s.message,
                })
        else:
            snapshots.append({
                "id": reminder.id, "fire_at": reminder.fire_at.isoformat(),
                "status": reminder.status, "message": reminder.message,
            })
    return snapshots


def undo_reschedule(db: Session, payload: dict) -> str:
    """Reverse a reschedule by restoring previous fire_at/status/message."""
    import logging
    log = logging.getLogger(__name__)
    for snap in payload.get("undo_state", []):
        r = db.query(Reminder).filter(Reminder.id == snap["id"]).first()
        if r:
            r.fire_at = _parse_dt(snap["fire_at"])
            r.status = snap["status"]
            r.message = snap["message"]
    db.commit()
    label = payload.get("description") or "reminder"
    log.info("Undid reschedule for: %s", label)
    return f"Undone! \"{label}\" restored to its previous time."


def execute_cancel(db: Session, payload: dict) -> str:
    """Execute a confirmed cancel action. Called after user replies YES."""
    import logging
    log = logging.getLogger(__name__)

    matched_id = payload["matched_id"]
    matched_type = payload["matched_type"]

    if matched_type in ("reminder", "recurring"):
        reminder = db.query(Reminder).filter(
            Reminder.id == matched_id,
            Reminder.status.in_(["pending", "sent"]),
        ).first()
        if not reminder:
            return "That reminder no longer exists."
        if reminder.parent_event_id:
            for s in db.query(Reminder).filter(
                Reminder.parent_event_id == reminder.parent_event_id,
                Reminder.status.in_(["pending", "sent"]),
            ).all():
                s.status = "cancelled"
        else:
            reminder.status = "cancelled"
        db.commit()
        log.info("Cancelled reminder #%d: %s", reminder.id, reminder.label)
        return f"Cancelled: \"{reminder.label}\""

    elif matched_type == "nag":
        nag = db.query(NagSchedule).filter(
            NagSchedule.id == matched_id,
            NagSchedule.status == "active",
        ).first()
        if not nag:
            return "That nag no longer exists."
        nag.status = "deleted"
        nag.completed_at = datetime.now(timezone.utc)
        db.commit()
        log.info("Cancelled nag #%d: %s", nag.id, nag.label)
        return f"Cancelled: \"{nag.label}\""

    return "Unknown item type."


def _capture_cancel_undo(db: Session, matched_id: int, matched_type: str) -> dict:
    """Capture current state before a cancel, for undo."""
    if matched_type in ("reminder", "recurring"):
        reminder = db.query(Reminder).filter(Reminder.id == matched_id).first()
        if not reminder:
            return {}
        if reminder.parent_event_id:
            siblings = db.query(Reminder).filter(
                Reminder.parent_event_id == reminder.parent_event_id,
                Reminder.status.in_(["pending", "sent"]),
            ).all()
            return {"items": [{"id": s.id, "prev_status": s.status} for s in siblings]}
        return {"items": [{"id": reminder.id, "prev_status": reminder.status}]}
    elif matched_type == "nag":
        nag = db.query(NagSchedule).filter(NagSchedule.id == matched_id).first()
        if not nag:
            return {}
        return {"nag_id": nag.id, "prev_status": nag.status,
                "prev_completed_at": nag.completed_at.isoformat() if nag.completed_at else None}
    return {}


def undo_cancel(db: Session, payload: dict) -> str:
    """Reverse a cancel by restoring previous statuses."""
    import logging
    log = logging.getLogger(__name__)
    undo = payload.get("undo_state", {})
    label = payload.get("label", "item")

    # Undo reminder cancellation
    for item in undo.get("items", []):
        r = db.query(Reminder).filter(Reminder.id == item["id"]).first()
        if r:
            r.status = item["prev_status"]

    # Undo nag cancellation
    if "nag_id" in undo:
        nag = db.query(NagSchedule).filter(NagSchedule.id == undo["nag_id"]).first()
        if nag:
            nag.status = undo["prev_status"]
            nag.completed_at = _parse_dt(undo["prev_completed_at"]) if undo.get("prev_completed_at") else None

    db.commit()
    log.info("Undid cancel for: %s", label)
    return f"Undone! \"{label}\" has been restored."


def execute_acknowledge(db: Session, payload: dict) -> str:
    """Execute an acknowledge action."""
    import logging
    log = logging.getLogger(__name__)

    matched_id = payload["matched_id"]
    matched_type = payload["matched_type"]
    now = datetime.now(timezone.utc)

    if matched_type == "nag":
        nag = db.query(NagSchedule).filter(
            NagSchedule.id == matched_id,
            NagSchedule.status == "active",
        ).first()
        if not nag:
            return "That nag no longer exists."
        if nag.repeating:
            nag.active_since = None
            nag.nag_until = None
            nag.nag_count = 0
            nag.next_nag_at = _next_nag_cycle(nag, now)
            db.commit()
            log.info("Acknowledged nag #%d: %s", nag.id, nag.label)
            return f"Got it! \"{nag.label}\" done. Next cycle: {_format_time(nag.next_nag_at)}"
        else:
            nag.status = "deleted"
            nag.completed_at = now
            db.commit()
            log.info("Acknowledged nag #%d (one-time, now deleted): %s", nag.id, nag.label)
            return f"Got it! \"{nag.label}\" done."

    elif matched_type == "reminder":
        reminder = db.query(Reminder).filter(
            Reminder.id == matched_id,
            Reminder.status.in_(["pending", "sent"]),
        ).first()
        if not reminder:
            return "That reminder no longer exists."
        if reminder.parent_event_id:
            for s in db.query(Reminder).filter(
                Reminder.parent_event_id == reminder.parent_event_id,
                Reminder.status.in_(["pending", "sent"]),
            ).all():
                s.status = "dismissed"
        else:
            reminder.status = "dismissed"
        db.commit()
        log.info("Dismissed reminder #%d: %s", reminder.id, reminder.label)
        return f"Dismissed: \"{reminder.label}\""

    return "Unknown item type."


def execute_acknowledge_all(db: Session, payload: dict) -> str:
    """Execute a confirmed acknowledge-all action. Called after user replies YES."""
    import logging
    log = logging.getLogger(__name__)

    now = datetime.now(timezone.utc)

    active_nags = db.query(NagSchedule).filter(
        NagSchedule.user_phone == USER_PHONE,
        NagSchedule.status == "active",
        NagSchedule.active_since.isnot(None),
    ).all()
    for nag in active_nags:
        if nag.repeating:
            nag.active_since = None
            nag.nag_until = None
            nag.nag_count = 0
            nag.next_nag_at = _next_nag_cycle(nag, now)
        else:
            nag.status = "deleted"
            nag.completed_at = now

    reminders = db.query(Reminder).filter(
        Reminder.user_phone == USER_PHONE,
        Reminder.status.in_(["pending", "sent"]),
    ).all()
    for r in reminders:
        r.status = "dismissed"

    db.commit()
    total = len(active_nags) + len(reminders)
    log.info("Acknowledged all: %d nags, %d reminders", len(active_nags), len(reminders))
    return f"Cleared all! Marked {total} items as done/dismissed."


def _capture_acknowledge_undo(db: Session, matched_id: int, matched_type: str) -> dict:
    """Capture current state before an acknowledge, for undo."""
    if matched_type == "nag":
        nag = db.query(NagSchedule).filter(NagSchedule.id == matched_id).first()
        if not nag:
            return {}
        return {
            "nag_id": nag.id, "repeating": nag.repeating,
            "prev_status": nag.status,
            "prev_active_since": nag.active_since.isoformat() if nag.active_since else None,
            "prev_nag_until": nag.nag_until.isoformat() if nag.nag_until else None,
            "prev_nag_count": nag.nag_count,
            "prev_next_nag_at": nag.next_nag_at.isoformat() if nag.next_nag_at else None,
            "prev_completed_at": nag.completed_at.isoformat() if nag.completed_at else None,
        }
    elif matched_type == "reminder":
        reminder = db.query(Reminder).filter(Reminder.id == matched_id).first()
        if not reminder:
            return {}
        if reminder.parent_event_id:
            siblings = db.query(Reminder).filter(
                Reminder.parent_event_id == reminder.parent_event_id,
                Reminder.status.in_(["pending", "sent"]),
            ).all()
            return {"items": [{"id": s.id, "prev_status": s.status} for s in siblings]}
        return {"items": [{"id": reminder.id, "prev_status": reminder.status}]}
    return {}


def undo_acknowledge(db: Session, payload: dict) -> str:
    """Reverse an acknowledge by restoring previous state."""
    import logging
    log = logging.getLogger(__name__)
    undo = payload.get("undo_state", {})
    label = payload.get("label", "item")

    if "nag_id" in undo:
        nag = db.query(NagSchedule).filter(NagSchedule.id == undo["nag_id"]).first()
        if nag:
            nag.status = undo["prev_status"]
            nag.active_since = _parse_dt(undo["prev_active_since"]) if undo.get("prev_active_since") else None
            nag.nag_until = _parse_dt(undo["prev_nag_until"]) if undo.get("prev_nag_until") else None
            nag.nag_count = undo.get("prev_nag_count", 0)
            nag.next_nag_at = _parse_dt(undo["prev_next_nag_at"]) if undo.get("prev_next_nag_at") else None
            nag.completed_at = _parse_dt(undo["prev_completed_at"]) if undo.get("prev_completed_at") else None

    for item in undo.get("items", []):
        r = db.query(Reminder).filter(Reminder.id == item["id"]).first()
        if r:
            r.status = item["prev_status"]

    db.commit()
    log.info("Undid acknowledge for: %s", label)
    return f"Undone! \"{label}\" restored."


def _capture_acknowledge_all_undo(db: Session) -> dict:
    """Capture state of all items before acknowledge-all, for undo."""
    nags = []
    for n in db.query(NagSchedule).filter(
        NagSchedule.user_phone == USER_PHONE,
        NagSchedule.status == "active",
        NagSchedule.active_since.isnot(None),
    ).all():
        nags.append({
            "id": n.id, "repeating": n.repeating,
            "prev_status": n.status,
            "prev_active_since": n.active_since.isoformat() if n.active_since else None,
            "prev_nag_until": n.nag_until.isoformat() if n.nag_until else None,
            "prev_nag_count": n.nag_count,
            "prev_next_nag_at": n.next_nag_at.isoformat() if n.next_nag_at else None,
            "prev_completed_at": n.completed_at.isoformat() if n.completed_at else None,
        })

    reminders = []
    for r in db.query(Reminder).filter(
        Reminder.user_phone == USER_PHONE,
        Reminder.status.in_(["pending", "sent"]),
    ).all():
        reminders.append({"id": r.id, "prev_status": r.status})

    return {"nags": nags, "reminders": reminders}


def undo_acknowledge_all(db: Session, payload: dict) -> str:
    """Reverse an acknowledge-all by restoring all previous states."""
    import logging
    log = logging.getLogger(__name__)
    undo = payload.get("undo_state", {})

    for snap in undo.get("nags", []):
        nag = db.query(NagSchedule).filter(NagSchedule.id == snap["id"]).first()
        if nag:
            nag.status = snap["prev_status"]
            nag.active_since = _parse_dt(snap["prev_active_since"]) if snap.get("prev_active_since") else None
            nag.nag_until = _parse_dt(snap["prev_nag_until"]) if snap.get("prev_nag_until") else None
            nag.nag_count = snap.get("prev_nag_count", 0)
            nag.next_nag_at = _parse_dt(snap["prev_next_nag_at"]) if snap.get("prev_next_nag_at") else None
            nag.completed_at = _parse_dt(snap["prev_completed_at"]) if snap.get("prev_completed_at") else None

    for snap in undo.get("reminders", []):
        r = db.query(Reminder).filter(Reminder.id == snap["id"]).first()
        if r:
            r.status = snap["prev_status"]

    db.commit()
    total = len(undo.get("nags", [])) + len(undo.get("reminders", []))
    log.info("Undid acknowledge-all: %d items restored", total)
    return f"Undone! Restored {total} items."


def _handle_create_nag(db: Session, data: dict) -> str:
    label = data.get("label", "Nag")
    message = data.get("message", f"Reminder: {label}")
    cron_expr = data.get("cron_expression", "")
    interval = data.get("interval_minutes", 15)
    max_dur = data.get("max_duration_minutes")
    repeating = data.get("repeating", False)
    anchor = data.get("anchor_to_completion", False)
    if anchor:
        repeating = True
    cycle_months = data.get("cycle_months")
    cycle_days = data.get("cycle_days")
    first_nag_at = data.get("first_nag_at")
    user_specified_time = data.get("user_specified_time", True)
    recurrence_desc = data.get("recurrence_description")
    deadline_at_str = data.get("deadline_at")
    min_interval = data.get("min_interval_minutes")

    # Default cron if none provided
    if not cron_expr:
        cron_expr = "0 12 * * *"

    # Parse deadline
    deadline_at = _parse_dt(deadline_at_str) if deadline_at_str else None
    now = datetime.now(timezone.utc)

    if deadline_at:
        # Deadline nags start active immediately, no cron cycling
        next_fire = now
        # No max_duration — the nag runs until done or cancelled
        max_dur = None
    else:
        # Auto-default max_duration_minutes for repeating nags to prevent infinite nagging.
        # Exception: completion-anchored nags should nag indefinitely until acknowledged.
        if repeating and max_dur is None and not anchor:
            cron_dow = cron_expr.split()[4] if len(cron_expr.split()) >= 5 else "*"
            if cycle_months or "monthly" in (recurrence_desc or "").lower():
                max_dur = 2880   # 48 hours for monthly
            elif cycle_days and cycle_days >= 7:
                max_dur = 1440   # 24 hours for weekly
            elif cron_dow not in ("*", "0-6", "0,1,2,3,4,5,6"):
                max_dur = 720    # 12 hours for weekday/partial-week
            else:
                max_dur = 720    # 12 hours default for daily

        if first_nag_at:
            next_fire = _parse_dt(first_nag_at)
        elif not user_specified_time:
            next_fire = _random_nag_time()
        else:
            next_fire = _next_cron_fire(cron_expr, USER_TIMEZONE)

    nag = NagSchedule(
        user_phone=USER_PHONE,
        label=label,
        message=message,
        cron_expression=cron_expr,
        interval_minutes=interval,
        max_duration_minutes=max_dur,
        repeating=repeating,
        recurrence_description=recurrence_desc,
        timezone=USER_TIMEZONE,
        next_nag_at=next_fire,
        anchor_to_completion=anchor,
        cycle_months=cycle_months,
        cycle_days=cycle_days,
        deadline_at=deadline_at,
        min_interval_minutes=min_interval,
        status="active",
    )
    if deadline_at:
        nag.active_since = now
        nag.nag_count = 0
    db.add(nag)
    db.commit()

    # Build confirmation message
    if deadline_at:
        past_warning = " (deadline already passed — nagging at max frequency!)" if deadline_at <= now else ""
        parts = [f"Deadline nag set: \"{label}\" due {_format_time(deadline_at)}{past_warning}"]
        if min_interval:
            parts.append(f" (min interval: {min_interval}min)")
    else:
        parts = [f"Nag set: \"{label}\" every {interval} min"]
        if recurrence_desc:
            parts.append(f", {recurrence_desc}")
        if anchor:
            period = f"{cycle_months} month(s)" if cycle_months else f"{cycle_days} day(s)"
            parts.append(f", next cycle {period} after completion")
    parts.append(f". First: {_format_time(next_fire)}")
    return "".join(parts)


def _handle_acknowledge(db: Session, data: dict) -> str:
    import json as _json
    import logging
    from app.openai_client import deduce_acknowledge_target

    log = logging.getLogger(__name__)

    keyword = data.get("keyword")
    ack_all = data.get("all", False)
    now = datetime.now(timezone.utc)

    if ack_all:
        # Capture undo state for all items before executing
        undo_state = _capture_acknowledge_all_undo(db)
        if not undo_state["nags"] and not undo_state["reminders"]:
            return "Nothing pending to mark as done!"

        reply = execute_acknowledge_all(db, {})

        db.query(PendingConfirmation).filter(PendingConfirmation.user_phone == USER_PHONE).delete()
        db.add(PendingConfirmation(
            user_phone=USER_PHONE,
            action_type="undo_acknowledge_all",
            payload=_json.dumps({"undo_state": undo_state}),
        ))
        db.commit()
        return f"{reply} Reply UNDO to reverse."

    # Check if the raw message has meaningful keywords even if the parser didn't extract one
    raw_message = data.get("_raw_message", "")
    raw_keywords = [w.lower() for w in raw_message.split() if w.lower() not in _ACK_STOP_WORDS and len(w) > 1]
    if not keyword and raw_keywords:
        keyword = " ".join(raw_keywords)

    # No keyword — pick most recent active nag, then sent reminder
    if not keyword:
        nag = db.query(NagSchedule).filter(
            NagSchedule.user_phone == USER_PHONE,
            NagSchedule.status == "active",
            NagSchedule.active_since.isnot(None),
        ).order_by(NagSchedule.next_nag_at.asc()).first()

        if nag:
            match = {"id": nag.id, "type": "nag", "label": nag.label}
        else:
            reminder = db.query(Reminder).filter(
                Reminder.user_phone == USER_PHONE,
                Reminder.status == "sent",
            ).order_by(Reminder.sent_at.desc()).first()

            if reminder:
                match = {"id": reminder.id, "type": "reminder", "label": reminder.label}
            else:
                return "Nothing pending to mark as done!"
    else:
        # Keyword provided — gather all acknowledgeable items and GPT fuzzy match
        ack_items = []

        for n in db.query(NagSchedule).filter(
            NagSchedule.user_phone == USER_PHONE,
            NagSchedule.status == "active",
        ).all():
            state = "ACTIVE" if n.active_since else "waiting"
            ack_items.append({"id": n.id, "type": "nag", "label": n.label,
                              "detail": f"every {n.interval_minutes}min [{state}]",
                              "message": n.message})

        for r in db.query(Reminder).filter(
            Reminder.user_phone == USER_PHONE,
            Reminder.status.in_(["pending", "sent"]),
        ).order_by(Reminder.created_at.desc()).all():
            ack_items.append({"id": r.id, "type": "reminder", "label": r.label,
                              "detail": f"fires {_format_time(r.fire_at)} [{r.status}]",
                              "message": r.message})

        if not ack_items:
            return "Nothing pending to mark as done!"

        original_message = data.get("_raw_message") or keyword

        match = _keyword_prefilter(original_message, ack_items, _ACK_STOP_WORDS)
        if not match:
            result = deduce_acknowledge_target(original_message, ack_items)
            log.info("Acknowledge match result: %s", result)

            if not result.get("matched_id"):
                return f"Couldn't find anything matching \"{keyword}\". Text LIST to see your items."

            try:
                matched_id = int(result["matched_id"])
            except (ValueError, TypeError):
                return f"Couldn't find anything matching \"{keyword}\". Text LIST to see your items."

            match = next((i for i in ack_items if i["id"] == matched_id and i["type"] == result.get("matched_type")), None)
            if not match:
                return f"Couldn't find anything matching \"{keyword}\". Text LIST to see your items."

    # Execute immediately and store undo state
    payload = {"matched_id": match["id"], "matched_type": match["type"], "label": match["label"]}
    undo_state = _capture_acknowledge_undo(db, match["id"], match["type"])
    reply = execute_acknowledge(db, payload)

    undo_payload = {**payload, "undo_state": undo_state}
    db.query(PendingConfirmation).filter(PendingConfirmation.user_phone == USER_PHONE).delete()
    db.add(PendingConfirmation(
        user_phone=USER_PHONE,
        action_type="undo_acknowledge",
        payload=_json.dumps(undo_payload),
    ))
    db.commit()

    return f"{reply} Reply UNDO to reverse."


def _handle_cancel(db: Session, data: dict) -> str:
    import json as _json
    import logging
    from app.openai_client import deduce_cancel_target

    log = logging.getLogger(__name__)

    keyword = data.get("keyword")
    target_type = data.get("type")  # "reminder", "recurring", "nag", or "action"

    # Gather all cancellable items
    items = []

    if target_type in (None, "reminder", "recurring"):
        for r in db.query(Reminder).filter(
            Reminder.user_phone == USER_PHONE,
            Reminder.status.in_(["pending", "sent"]),
        ).order_by(Reminder.created_at.desc()).all():
            rtype = "recurring" if r.cron_expression else "reminder"
            detail = f"({r.cron_expression}) next: {_format_time(r.fire_at)}" if r.cron_expression else f"fires {_format_time(r.fire_at)}"
            items.append({"id": r.id, "type": rtype, "label": r.label,
                          "detail": detail,
                          "message": r.message})

    if target_type in (None, "nag", "action"):
        for n in db.query(NagSchedule).filter(
            NagSchedule.user_phone == USER_PHONE,
            NagSchedule.status == "active",
        ).order_by(NagSchedule.created_at.desc()).all():
            state = "ACTIVE" if n.active_since else "waiting"
            items.append({"id": n.id, "type": "nag", "label": n.label,
                          "detail": f"every {n.interval_minutes}min [{state}], next: {_format_time(n.next_nag_at)}",
                          "message": n.message})

    if not items:
        return "Nothing to cancel!"

    original_message = data.get("_raw_message") or keyword

    # Only default to most-recent if user literally just said "cancel" with no context
    if not keyword and not original_message:
        match = items[0]
    elif len(items) == 1:
        # Only one cancellable item — just pick it
        match = items[0]
    else:
        # Try fast keyword matching first — only call GPT if ambiguous
        search_text = original_message or keyword
        match = _keyword_prefilter(search_text, items, _CANCEL_STOP_WORDS)
        if not match:
            result = deduce_cancel_target(search_text, items)
            log.info("Cancel match result: %s", result)

            if not result.get("matched_id"):
                return f"Couldn't find anything matching \"{keyword or original_message}\". Text LIST to see your items."

            try:
                matched_id = int(result["matched_id"])
            except (ValueError, TypeError):
                return f"Couldn't find anything matching \"{keyword or original_message}\". Text LIST to see your items."

            match = next((i for i in items if i["id"] == matched_id and i["type"] == result.get("matched_type")), None)
            if not match:
                return f"Couldn't find anything matching \"{keyword or original_message}\". Text LIST to see your items."

    # Execute immediately and store undo state
    payload = {"matched_id": match["id"], "matched_type": match["type"], "label": match["label"]}
    undo_state = _capture_cancel_undo(db, match["id"], match["type"])
    reply = execute_cancel(db, payload)

    undo_payload = {**payload, "undo_state": undo_state}
    db.query(PendingConfirmation).filter(PendingConfirmation.user_phone == USER_PHONE).delete()
    db.add(PendingConfirmation(
        user_phone=USER_PHONE,
        action_type="undo_cancel",
        payload=_json.dumps(undo_payload),
    ))
    db.commit()

    return f"{reply}. Reply UNDO to reverse."


def _handle_snooze(db: Session, data: dict) -> str:
    import json as _json
    import logging
    import re
    from app.openai_client import deduce_acknowledge_target

    log = logging.getLogger(__name__)

    duration = data.get("duration_minutes")
    raw_message = data.get("_raw_message", "")

    # Fallback: parse duration from raw message if GPT missed it
    if not duration:
        duration = _parse_snooze_duration(raw_message)
    duration = min(duration, 1440)  # cap at 24 hours

    keyword = data.get("keyword")
    now = datetime.now(timezone.utc)

    # Extract keywords from raw message if parser missed them
    raw_keywords = [w.lower() for w in raw_message.split() if w.lower() not in _SNOOZE_STOP_WORDS and len(w) > 1]
    if not keyword and raw_keywords:
        keyword = " ".join(raw_keywords)

    # No keyword — pick most recent active nag, then pending reminder
    if not keyword:
        nag = db.query(NagSchedule).filter(
            NagSchedule.user_phone == USER_PHONE,
            NagSchedule.status == "active",
            NagSchedule.active_since.isnot(None),
        ).order_by(NagSchedule.next_nag_at.asc()).first()

        if nag:
            match = {"id": nag.id, "type": "nag", "label": nag.label}
        else:
            reminder = db.query(Reminder).filter(
                Reminder.user_phone == USER_PHONE,
                Reminder.status.in_(["pending", "sent"]),
            ).order_by(Reminder.fire_at.asc()).first()

            if reminder:
                match = {"id": reminder.id, "type": "reminder", "label": reminder.label}
            else:
                return "Nothing to snooze!"
    else:
        # Keyword provided — gather all snoozeable items and match
        items = []

        for n in db.query(NagSchedule).filter(
            NagSchedule.user_phone == USER_PHONE,
            NagSchedule.status == "active",
        ).all():
            state = "ACTIVE" if n.active_since else "waiting"
            items.append({"id": n.id, "type": "nag", "label": n.label,
                           "detail": f"every {n.interval_minutes}min [{state}]",
                           "message": n.message})

        for r in db.query(Reminder).filter(
            Reminder.user_phone == USER_PHONE,
            Reminder.status.in_(["pending", "sent"]),
        ).order_by(Reminder.fire_at.asc()).all():
            items.append({"id": r.id, "type": "reminder", "label": r.label,
                           "detail": f"fires {_format_time(r.fire_at)} [{r.status}]",
                           "message": r.message})

        if not items:
            return "Nothing to snooze!"

        original_message = data.get("_raw_message") or keyword

        match = _keyword_prefilter(original_message, items, _SNOOZE_STOP_WORDS)
        if not match:
            result = deduce_acknowledge_target(original_message, items)
            log.info("Snooze match result: %s", result)

            if not result.get("matched_id"):
                return f"Couldn't find anything matching \"{keyword}\". Text LIST to see your items."

            try:
                matched_id = int(result["matched_id"])
            except (ValueError, TypeError):
                return f"Couldn't find anything matching \"{keyword}\". Text LIST to see your items."

            match = next((i for i in items if i["id"] == matched_id and i["type"] == result.get("matched_type")), None)
            if not match:
                return f"Couldn't find anything matching \"{keyword}\". Text LIST to see your items."

    # Execute immediately and store undo state
    payload = {"matched_id": match["id"], "matched_type": match["type"], "label": match["label"], "duration_minutes": duration}
    undo_state = _capture_snooze_undo(db, match["id"], match["type"])
    reply = execute_snooze(db, payload)

    undo_payload = {**payload, "undo_state": undo_state}
    db.query(PendingConfirmation).filter(PendingConfirmation.user_phone == USER_PHONE).delete()
    db.add(PendingConfirmation(
        user_phone=USER_PHONE,
        action_type="undo_snooze",
        payload=_json.dumps(undo_payload),
    ))
    db.commit()

    return f"{reply} Reply UNDO to reverse."


def execute_snooze(db: Session, payload: dict) -> str:
    """Execute a confirmed snooze action. Called after user replies YES."""
    import logging
    log = logging.getLogger(__name__)

    matched_id = payload["matched_id"]
    matched_type = payload["matched_type"]
    duration = payload.get("duration_minutes", 60)
    now = datetime.now(timezone.utc)
    snooze_until = now + timedelta(minutes=duration)

    if matched_type == "nag":
        nag = db.query(NagSchedule).filter(
            NagSchedule.id == matched_id,
            NagSchedule.status == "active",
        ).first()
        if not nag:
            return "That nag no longer exists."
        nag.next_nag_at = snooze_until
        db.commit()
        log.info("Snoozed nag #%d for %d min: %s", nag.id, duration, nag.label)
        return f"Snoozed \"{nag.label}\" for {duration} min."

    elif matched_type == "reminder":
        reminder = db.query(Reminder).filter(
            Reminder.id == matched_id,
            Reminder.status.in_(["pending", "sent"]),
        ).first()
        if not reminder:
            return "That reminder no longer exists."
        reminder.fire_at = snooze_until
        reminder.status = "pending"
        db.commit()
        log.info("Snoozed reminder #%d for %d min: %s", reminder.id, duration, reminder.label)
        return f"Snoozed \"{reminder.label}\" for {duration} min."

    return "Unknown item type."


def _capture_snooze_undo(db: Session, matched_id: int, matched_type: str) -> dict:
    """Capture current state before a snooze, for undo."""
    if matched_type == "nag":
        nag = db.query(NagSchedule).filter(NagSchedule.id == matched_id).first()
        if nag:
            return {"nag_id": nag.id, "prev_next_nag_at": nag.next_nag_at.isoformat() if nag.next_nag_at else None}
    elif matched_type == "reminder":
        r = db.query(Reminder).filter(Reminder.id == matched_id).first()
        if r:
            return {"reminder_id": r.id, "prev_fire_at": r.fire_at.isoformat(), "prev_status": r.status}
    return {}


def undo_snooze(db: Session, payload: dict) -> str:
    """Reverse a snooze by restoring previous timing."""
    import logging
    log = logging.getLogger(__name__)
    undo = payload.get("undo_state", {})
    label = payload.get("label", "item")

    if "nag_id" in undo:
        nag = db.query(NagSchedule).filter(NagSchedule.id == undo["nag_id"]).first()
        if nag and undo.get("prev_next_nag_at"):
            nag.next_nag_at = _parse_dt(undo["prev_next_nag_at"])

    if "reminder_id" in undo:
        r = db.query(Reminder).filter(Reminder.id == undo["reminder_id"]).first()
        if r:
            r.fire_at = _parse_dt(undo["prev_fire_at"])
            r.status = undo["prev_status"]

    db.commit()
    log.info("Undid snooze for: %s", label)
    return f"Undone! \"{label}\" restored to its previous time."


def _handle_list(db: Session, data: dict) -> str:
    lines = []

    reminders = db.query(Reminder).filter(
        Reminder.user_phone == USER_PHONE,
        Reminder.status.in_(["pending", "sent"]),
    ).order_by(Reminder.fire_at.asc()).all()
    one_shot = [r for r in reminders if not r.cron_expression]
    recurring = [r for r in reminders if r.cron_expression]
    if one_shot:
        lines.append("REMINDERS:")
        for r in one_shot:
            lines.append(f"  - {r.label} @ {_format_time(r.fire_at)} [{r.status}]")
    if recurring:
        lines.append("RECURRING:")
        for r in recurring:
            lines.append(f"  - {r.label} ({r.cron_expression}) next: {_format_time(r.fire_at)}")

    nags = db.query(NagSchedule).filter(
        NagSchedule.user_phone == USER_PHONE,
        NagSchedule.status == "active",
    ).order_by(NagSchedule.next_nag_at.asc()).all()
    if nags:
        lines.append("NAGS:")
        for n in nags:
            state = "ACTIVE" if n.active_since else "waiting"
            recurrence = f" ({n.recurrence_description})" if n.recurrence_description else ""
            src = f" [from: {n.source}]" if n.source else ""
            if n.deadline_at:
                interval_desc = f" deadline: {_format_time(n.deadline_at)}"
            else:
                interval_desc = f" every {n.interval_minutes}min"
            lines.append(f"  - {n.label}{interval_desc}{recurrence} [{state}]{src} (next: {_format_time(n.next_nag_at)})")

    if not lines:
        return "All clear! Nothing pending."

    return "\n".join(lines)


def _handle_briefing(db: Session, data: dict) -> str:
    from app.morning_briefing import generate_morning_briefing
    try:
        return generate_morning_briefing()
    except Exception:
        return "Sorry, couldn't generate your briefing right now. Try again in a bit."


def _handle_log_exercise(db: Session, data: dict) -> str:
    from app.openai_client import _chat

    activity = data.get("activity", "exercise")
    duration = data.get("duration_minutes")
    distance = data.get("distance_miles")
    notes = data.get("notes")

    log_entry = ExerciseLog(
        user_phone=USER_PHONE,
        activity=activity,
        duration_minutes=duration,
        distance_miles=distance,
        notes=notes,
    )
    db.add(log_entry)
    db.commit()

    # Build a detail string for GPT to personalize the congrats
    parts = [activity]
    if distance:
        parts.append(f"{distance} miles")
    if duration:
        parts.append(f"{duration} minutes")
    if notes:
        parts.append(notes)
    detail = ", ".join(parts)

    reply = _chat(
        [
            {
                "role": "system",
                "content": "You are a supportive fitness buddy replying via SMS. "
                "Write a short (1-2 sentences, under 160 characters) congratulatory message "
                "about the exercise activity described. Be enthusiastic and personal.",
            },
            {"role": "user", "content": f"I just did: {detail}"},
        ],
        temperature=0.8,
    )
    return reply


def _handle_exercise_history(db: Session, data: dict) -> str:
    from datetime import date as _date
    from zoneinfo import ZoneInfo

    tz = ZoneInfo(USER_TIMEZONE)
    today = _date.today()

    try:
        start = _date.fromisoformat(data["start_date"])
    except (KeyError, ValueError, TypeError):
        start = today - timedelta(days=7)

    try:
        end = _date.fromisoformat(data["end_date"])
    except (KeyError, ValueError, TypeError):
        end = today

    # Convert date range to timezone-aware datetimes spanning the full days
    start_dt = datetime(start.year, start.month, start.day, tzinfo=tz).astimezone(timezone.utc)
    end_dt = datetime(end.year, end.month, end.day, 23, 59, 59, tzinfo=tz).astimezone(timezone.utc)

    entries = db.query(ExerciseLog).filter(
        ExerciseLog.user_phone == USER_PHONE,
        ExerciseLog.created_at >= start_dt,
        ExerciseLog.created_at <= end_dt,
    ).order_by(ExerciseLog.created_at.asc()).all()

    if not entries:
        return "No exercise activities found in that range."

    lines = [f"Exercise log ({start.strftime('%b %d')} - {end.strftime('%b %d')}):"]
    for e in entries:
        local_dt = e.created_at.astimezone(tz)
        parts = [e.activity]
        if e.distance_miles:
            parts.append(f"{e.distance_miles} mi")
        if e.duration_minutes:
            parts.append(f"{e.duration_minutes} min")
        if e.notes:
            parts.append(e.notes)
        lines.append(f"  {local_dt.strftime('%b %d')}: {', '.join(parts)}")

    return "\n".join(lines)


def _handle_help(db: Session, data: dict) -> str:
    return (
        "SMS ADHD Assistant commands:\n"
        "- Set a reminder: \"meeting at 4pm friday about X\"\n"
        "- Recurring reminder: \"remind me about Dr Watson every Tuesday at 3pm\"\n"
        "- Nag: \"nag me to enter my time at 9am every 15 min weekdays\"\n"
        "- Mark done: \"done\" or \"done [keyword]\"\n"
        "- Clear all: \"done all\"\n"
        "- Reschedule: \"move meeting to 3pm\" or \"reschedule dentist to friday\"\n"
        "- Cancel: \"cancel [keyword]\" or \"nevermind\"\n"
        "- Snooze: \"snooze\" or \"snooze 30\" (minutes)\n"
        "- Log exercise: \"I ran a mile in 9 min\" or \"I biked for 20 min\"\n"
        "- Exercise history: \"what exercise did I do this week\"\n"
        "- Morning briefing: \"briefing\" or \"what's my day look like\"\n"
        "- See pending: \"list\"\n"
        "- This message: \"commands\" or \"info\""
    )
