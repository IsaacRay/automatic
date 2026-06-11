"""Map parsed intents to DB operations and reply text."""

import random
import uuid
from datetime import datetime, timedelta, timezone

from sqlalchemy.orm import Session

from app.models import PendingConfirmation, NagSchedule, ScheduledFlash
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


def _validate_cron_expr(cron_expr: str) -> str | None:
    """Return an error message if the cron expression is unusable, else None.

    Catches the DOM+DOW both-restricted case, which croniter ORs — a trap
    GPT falls into when approximating patterns cron can't natively express
    (e.g. 'last weekday before the 15th' → '0 15 10-14 * 1-5' fires every
    weekday because 10-14 OR Mon-Fri = Mon-Fri).
    """
    parts = cron_expr.split()
    if len(parts) != 5:
        return f"Cron expression has {len(parts)} fields, expected 5."
    dom, dow = parts[2], parts[4]
    if dom != "*" and dow != "*":
        return (
            "Cron can't restrict both day-of-month and day-of-week in the same "
            "expression (they get OR'd, not AND'd). Please rephrase — e.g. "
            "'last weekday of the month' uses 'LW' in the day-of-month slot."
        )
    return None


def _next_cron_fire(cron_expr: str, tz_name: str, after: datetime = None) -> datetime:
    """Compute the next fire time for a cron expression, returned as UTC.

    The next fire strictly after `after` (default: now) is returned.
    """
    from zoneinfo import ZoneInfo
    from croniter import croniter
    tz = ZoneInfo(tz_name)
    base = after.astimezone(tz) if after else datetime.now(tz)
    cron = croniter(cron_expr, base)
    next_local = cron.get_next(datetime)
    return next_local.astimezone(timezone.utc)


def _end_of_today_local(now: datetime, tz_name: str) -> datetime:
    """The last moment of `now`'s local day, used to skip same-day cron fires."""
    from zoneinfo import ZoneInfo
    return now.astimezone(ZoneInfo(tz_name)).replace(hour=23, minute=59, second=59, microsecond=0)


def _next_nag_cycle(nag, completion_time: datetime = None) -> datetime:
    """Compute the next cycle start for a nag schedule.

    If anchor_to_completion is True, the next cycle is relative to completion_time.
    Otherwise, falls back to the cron expression. Cron-based next cycles skip any
    remaining fire on the completion day, so checking a daily item off clears it
    from today's list until tomorrow.
    """
    after_today = _end_of_today_local(completion_time or datetime.now(timezone.utc), nag.timezone)
    if nag.anchor_to_completion and completion_time:
        from zoneinfo import ZoneInfo
        from dateutil.relativedelta import relativedelta
        local_completion = completion_time.astimezone(ZoneInfo(nag.timezone))
        if nag.cycle_months:
            next_local = local_completion + relativedelta(months=nag.cycle_months)
        elif nag.cycle_days:
            next_local = local_completion + timedelta(days=nag.cycle_days)
        else:
            return _next_cron_fire(nag.cron_expression, nag.timezone, after=after_today)
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
    return _next_cron_fire(nag.cron_expression, nag.timezone, after=after_today)



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
        "create_nag": _handle_create_nag,
        "flash_lights": _handle_flash_lights,
        "acknowledge": _handle_acknowledge,
        "cancel": _handle_cancel,
        "snooze": _handle_snooze,
        "list": _handle_list,
        "briefing": _handle_briefing,
        "help": _handle_help,
        "context_update": _handle_context_update,
    }
    handler = handlers.get(intent)
    if handler:
        return handler(db, data)
    return "I didn't understand that. Text COMMANDS to see what I can do."


def _apply_deadline_reply(db: Session, nag_id: int, reply_text: str) -> str:
    """Apply the user's reply to a 'When's the deadline?' follow-up (from a `.. `
    capture). Accepts a plain time ("3pm"), a recurrence ("daily at 3pm",
    "monthly on the 2nd, anchored to completion"), or 'none'/blank to keep the
    end-of-day default. Reconfigures the dormant nag accordingly and wakes it."""
    nag = db.query(NagSchedule).filter(
        NagSchedule.id == nag_id, NagSchedule.status == "active"
    ).first()
    if not nag:
        return "That item's no longer around."
    now = datetime.now(timezone.utc)

    def _activate_eod():
        # Wake the dormant one-shot so it nags from now (Zeno toward end of day).
        nag.active_since = now
        nag.nag_count = 0
        nag.next_nag_at = now

    low = reply_text.strip().lower()
    if low in {"none", "no", "skip", "eod", "end of day", ""}:
        _activate_eod()
        db.commit()
        return f'Got it — "{nag.label}" due by end of day.'

    try:
        from app.openai_client import parse_user_sms
        parsed = parse_user_sms("nag me to " + nag.label + " " + reply_text)
        data = parsed.get("data", {})
    except Exception:
        data = {}

    cron_expr = data.get("cron_expression") or None
    anchor = bool(data.get("anchor_to_completion", False))
    repeating = anchor or bool(cron_expr)

    if repeating:
        if not cron_expr:
            cron_expr = "0 12 * * *"
        if _validate_cron_expr(cron_expr):
            _activate_eod()
            db.commit()
            return f'Couldn\'t read that schedule — "{nag.label}" stays due end of day, starting now.'
        # Reconfigure the captured item as a recurring nag.
        nag.cron_expression = cron_expr
        nag.repeating = True
        nag.anchor_to_completion = anchor
        nag.cycle_months = data.get("cycle_months")
        nag.cycle_days = data.get("cycle_days")
        nag.recurrence_description = data.get("recurrence_description")
        nag.deadline_offset_minutes = data.get("deadline_offset_minutes") or _default_offset_to_eleven_pm(cron_expr)
        nag.min_interval_minutes = data.get("min_interval_minutes")
        nag.deadline_at = None  # recomputed each cycle by the scheduler
        nag.next_nag_at = _next_cron_fire(cron_expr, USER_TIMEZONE)  # first nag at start time
        nag.active_since = None  # dormant until the cron start fires
        nag.nag_count = 0
        db.commit()
        desc = nag.recurrence_description or "recurring"
        tail = ", anchored to completion" if anchor else ""
        return f'Set: "{nag.label}" — {desc}{tail}. First: {_format_time(nag.next_nag_at)}.'

    dstr = data.get("deadline_at")
    if dstr:
        nag.deadline_at = _parse_dt(dstr)
        nag.repeating = False
        _activate_eod()
        db.commit()
        return f'Deadline set: "{nag.label}" by {_format_time(nag.deadline_at)}.'

    _activate_eod()
    db.commit()
    return f'Couldn\'t read that time — "{nag.label}" stays due end of day, starting now.'


def _handle_context_update(db: Session, data: dict) -> str:
    """Record the user's current location/intent, then surface any items that
    now fit the moment."""
    from app.context_engine import set_user_context, evaluate_context

    text = (data.get("text") or data.get("raw") or "").strip()
    if not text:
        return "Got it."
    set_user_context(db, text)
    db.commit()
    try:
        surfaced = evaluate_context(db)
    except Exception:
        surfaced = []
    if surfaced:
        return "Got it — flagged " + ", ".join(surfaced) + " for now."
    return "Got it."


def execute_cancel(db: Session, payload: dict) -> str:
    """Execute a confirmed cancel action. Called after user replies YES."""
    import logging
    log = logging.getLogger(__name__)

    matched_id = payload["matched_id"]
    matched_type = payload["matched_type"]

    if matched_type == "nag":
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
    if matched_type == "nag":
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
            nag.deadline_at = None
            nag.nag_count = 0
            nag.snooze_count = 0
            nag.completed_at = now
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
            nag.deadline_at = None
            nag.nag_count = 0
            nag.snooze_count = 0
            nag.completed_at = now
            nag.next_nag_at = _next_nag_cycle(nag, now)
        else:
            nag.status = "deleted"
            nag.completed_at = now

    db.commit()
    total = len(active_nags)
    log.info("Acknowledged all: %d nags", len(active_nags))
    return f"Cleared all! Marked {total} items as done."


def reopen_nag(db: Session, nag_id: int) -> None:
    """Un-check a nag that was checked off today (UI uncheck). Clears the
    completion and surfaces it on today's list again, due now."""
    now = datetime.now(timezone.utc)
    nag = db.query(NagSchedule).filter(NagSchedule.id == nag_id).first()
    if not nag:
        return
    nag.status = "active"
    nag.completed_at = None
    nag.next_nag_at = now
    db.commit()


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
            "prev_deadline_at": nag.deadline_at.isoformat() if nag.deadline_at else None,
            "prev_nag_count": nag.nag_count,
            "prev_next_nag_at": nag.next_nag_at.isoformat() if nag.next_nag_at else None,
            "prev_completed_at": nag.completed_at.isoformat() if nag.completed_at else None,
        }
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
            nag.deadline_at = _parse_dt(undo["prev_deadline_at"]) if undo.get("prev_deadline_at") else None
            nag.nag_count = undo.get("prev_nag_count", 0)
            nag.next_nag_at = _parse_dt(undo["prev_next_nag_at"]) if undo.get("prev_next_nag_at") else None
            nag.completed_at = _parse_dt(undo["prev_completed_at"]) if undo.get("prev_completed_at") else None

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
            "prev_deadline_at": n.deadline_at.isoformat() if n.deadline_at else None,
            "prev_nag_count": n.nag_count,
            "prev_next_nag_at": n.next_nag_at.isoformat() if n.next_nag_at else None,
            "prev_completed_at": n.completed_at.isoformat() if n.completed_at else None,
        })

    return {"nags": nags}


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
            nag.deadline_at = _parse_dt(snap["prev_deadline_at"]) if snap.get("prev_deadline_at") else None
            nag.nag_count = snap.get("prev_nag_count", 0)
            nag.next_nag_at = _parse_dt(snap["prev_next_nag_at"]) if snap.get("prev_next_nag_at") else None
            nag.completed_at = _parse_dt(snap["prev_completed_at"]) if snap.get("prev_completed_at") else None

    db.commit()
    total = len(undo.get("nags", []))
    log.info("Undid acknowledge-all: %d items restored", total)
    return f"Undone! Restored {total} items."


def _end_of_day_local(dt_utc: datetime) -> datetime:
    """Return 11pm local on the calendar day of dt_utc (next day if already past 11pm)."""
    from zoneinfo import ZoneInfo
    tz = ZoneInfo(USER_TIMEZONE)
    local = dt_utc.astimezone(tz)
    eleven_pm = local.replace(hour=23, minute=0, second=0, microsecond=0)
    if eleven_pm <= local:
        eleven_pm += timedelta(days=1)
    return eleven_pm.astimezone(timezone.utc)


def _default_offset_to_eleven_pm(cron_expr: str) -> int:
    """For recurring nags: minutes from the cron's hour/minute to 23:00 (same day).
    Falls back to 720 (12h) when cron hour/minute are not simple integers."""
    parts = cron_expr.split()
    try:
        minute = int(parts[0])
        hour = int(parts[1])
    except (ValueError, IndexError):
        return 720
    cron_minutes = hour * 60 + minute
    eleven_pm = 23 * 60
    if cron_minutes < eleven_pm:
        return eleven_pm - cron_minutes
    return (24 * 60) - cron_minutes + eleven_pm


def _handle_create_nag(db: Session, data: dict) -> str:
    label = data.get("label", "Nag")
    message = data.get("message", f"Reminder: {label}")
    cron_expr = data.get("cron_expression") or None
    anchor = bool(data.get("anchor_to_completion", False))
    cycle_months = data.get("cycle_months")
    cycle_days = data.get("cycle_days")
    first_nag_at = data.get("first_nag_at")
    user_specified_time = data.get("user_specified_time", True)
    recurrence_desc = data.get("recurrence_description")
    deadline_at_str = data.get("deadline_at")
    deadline_offset_minutes = data.get("deadline_offset_minutes")
    min_interval = data.get("min_interval_minutes")

    cron_unsupported = data.get("cron_unsupported_reason")
    if cron_unsupported and not cron_expr:
        return (
            f"I couldn't turn that into a valid schedule — {cron_unsupported} "
            "isn't expressible in cron. Try rephrasing (e.g. 'last weekday of the month')."
        )

    repeating = anchor or bool(cron_expr)
    now = datetime.now(timezone.utc)

    if repeating:
        if not cron_expr:
            cron_expr = "0 12 * * *"
        cron_err = _validate_cron_expr(cron_expr)
        if cron_err:
            return f"Couldn't save that nag: {cron_err}"

        if not deadline_offset_minutes:
            deadline_offset_minutes = _default_offset_to_eleven_pm(cron_expr)

        if first_nag_at:
            next_fire = _parse_dt(first_nag_at)
        elif not user_specified_time:
            next_fire = _random_nag_time()
        else:
            next_fire = _next_cron_fire(cron_expr, USER_TIMEZONE)

        active_since = None
        deadline_at = None  # recomputed each cycle by the scheduler
    else:
        # One-shot deadline nag
        deadline_at = _parse_dt(deadline_at_str) if deadline_at_str else None
        if first_nag_at:
            # Explicit "start nagging at Z" — stay dormant until then
            next_fire = _parse_dt(first_nag_at)
            active_since = None
            anchor_time = next_fire
        else:
            next_fire = now
            active_since = now
            anchor_time = now
        if not deadline_at:
            deadline_at = _end_of_day_local(anchor_time)

    nag = NagSchedule(
        user_phone=USER_PHONE,
        label=label,
        message=message,
        cron_expression=cron_expr,
        repeating=repeating,
        recurrence_description=recurrence_desc,
        timezone=USER_TIMEZONE,
        next_nag_at=next_fire,
        anchor_to_completion=anchor,
        cycle_months=cycle_months,
        cycle_days=cycle_days,
        deadline_at=deadline_at,
        deadline_offset_minutes=deadline_offset_minutes,
        min_interval_minutes=min_interval,
        active_since=active_since,
        nag_count=0,
        status="active",
    )
    db.add(nag)
    db.commit()

    # Build confirmation message
    if repeating:
        hours, mins = divmod(deadline_offset_minutes, 60)
        offset_desc = f"{hours}h{mins:02d}m" if hours else f"{mins}m"
        parts = [f"Recurring nag set: \"{label}\" — deadline {offset_desc} after each cycle start"]
        if recurrence_desc:
            parts.append(f", {recurrence_desc}")
        if anchor:
            period = f"{cycle_months} month(s)" if cycle_months else f"{cycle_days} day(s)"
            parts.append(f", next cycle {period} after completion")
    else:
        past_warning = " (deadline already passed — nagging at max frequency!)" if deadline_at <= now else ""
        parts = [f"Nag set: \"{label}\" — deadline {_format_time(deadline_at)}{past_warning}"]
    if min_interval:
        parts.append(f" (min interval: {min_interval}min)")
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
        if not undo_state["nags"]:
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

    # No keyword — pick most recent active nag
    if not keyword:
        nag = db.query(NagSchedule).filter(
            NagSchedule.user_phone == USER_PHONE,
            NagSchedule.status == "active",
            NagSchedule.active_since.isnot(None),
        ).order_by(NagSchedule.next_nag_at.asc()).first()

        if nag:
            match = {"id": nag.id, "type": "nag", "label": nag.label}
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
                              "detail": f"deadline {_format_time(n.deadline_at)} [{state}]" if n.deadline_at else f"[{state}]",
                              "message": n.message})

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

    # Gather all cancellable items (nags)
    items = []
    for n in db.query(NagSchedule).filter(
        NagSchedule.user_phone == USER_PHONE,
        NagSchedule.status == "active",
    ).order_by(NagSchedule.created_at.desc()).all():
        state = "ACTIVE" if n.active_since else "waiting"
        deadline_str = f", deadline {_format_time(n.deadline_at)}" if n.deadline_at else ""
        items.append({"id": n.id, "type": "nag", "label": n.label,
                      "detail": f"[{state}]{deadline_str}, next: {_format_time(n.next_nag_at)}",
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


def _match_snooze_target(db: Session, raw_message: str) -> dict | None:
    """Resolve which active nag a snooze request refers to.

    With no usable keyword, picks the most imminent in-cycle nag. With a
    keyword, runs the keyword prefilter then GPT fuzzy match. Returns a dict
    {"id", "label", "snooze_count"} or None if nothing matches.
    """
    import logging
    from app.openai_client import deduce_acknowledge_target

    log = logging.getLogger(__name__)

    raw_keywords = [w.lower() for w in raw_message.split()
                    if w.lower() not in _SNOOZE_STOP_WORDS and len(w) > 1]

    # No keyword — pick the most imminent nag already in a cycle.
    if not raw_keywords:
        nag = db.query(NagSchedule).filter(
            NagSchedule.user_phone == USER_PHONE,
            NagSchedule.status == "active",
            NagSchedule.active_since.isnot(None),
        ).order_by(NagSchedule.next_nag_at.asc()).first()
        if nag:
            return {"id": nag.id, "label": nag.label, "snooze_count": nag.snooze_count or 0}
        return None

    # Keyword provided — gather all snoozeable items and match.
    items = []
    for n in db.query(NagSchedule).filter(
        NagSchedule.user_phone == USER_PHONE,
        NagSchedule.status == "active",
    ).all():
        state = "ACTIVE" if n.active_since else "waiting"
        items.append({"id": n.id, "type": "nag", "label": n.label,
                       "detail": f"deadline {_format_time(n.deadline_at)} [{state}]" if n.deadline_at else f"[{state}]",
                       "message": n.message, "snooze_count": n.snooze_count or 0})

    if not items:
        return None

    match = _keyword_prefilter(raw_message, items, _SNOOZE_STOP_WORDS)
    if not match:
        result = deduce_acknowledge_target(raw_message, items)
        log.info("Snooze match result: %s", result)
        try:
            matched_id = int(result.get("matched_id"))
        except (ValueError, TypeError):
            return None
        match = next((i for i in items if i["id"] == matched_id and i["type"] == result.get("matched_type")), None)
        if not match:
            return None

    return {"id": match["id"], "label": match["label"], "snooze_count": match.get("snooze_count", 0)}


def _handle_snooze(db: Session, data: dict) -> str:
    """Snooze intent ('later', 'not now', ...) — route into the negotiation."""
    return start_snooze_negotiation(db, data.get("_raw_message", ""))


def start_snooze_negotiation(db: Session, raw_message: str) -> str:
    """Begin a snooze negotiation. The bot resists the snooze for
    (snooze_count + 1) rounds, escalating its incredulity, before relenting.
    Sends the first push-back and stores the negotiation as a
    PendingConfirmation; subsequent replies are handled by
    `continue_snooze_negotiation`. If the item has never been snoozed it still
    gets one round of push-back.
    """
    import json as _json
    from app.openai_client import generate_snooze_resistance

    match = _match_snooze_target(db, raw_message)
    if not match:
        return "Couldn't find anything to snooze. Text LIST to see your items."

    duration = min(_parse_snooze_duration(raw_message), 1440)  # cap at 24 hours
    total_rounds = (match.get("snooze_count") or 0) + 1

    history = [{"role": "user", "content": raw_message or f"snooze {match['label']}"}]
    # Round 1 is the opening "snooze X" request — never a concession — so we only
    # use the push-back text.
    reply = generate_snooze_resistance(match["label"], 1, total_rounds, history)["reply"]
    history.append({"role": "assistant", "content": reply})

    payload = {
        "matched_id": match["id"],
        "matched_type": "nag",
        "label": match["label"],
        "duration_minutes": duration,
        "total_rounds": total_rounds,
        "rounds_remaining": total_rounds - 1,
        "history": history,
    }
    # One negotiation at a time.
    db.query(PendingConfirmation).filter(PendingConfirmation.user_phone == USER_PHONE).delete()
    db.add(PendingConfirmation(
        user_phone=USER_PHONE,
        action_type="snooze_negotiation",
        payload=_json.dumps(payload),
    ))
    db.commit()
    return reply


def continue_snooze_negotiation(db: Session, payload: dict, user_message: str) -> dict:
    """Handle one user reply in an ongoing snooze negotiation.

    Returns {"reply": str, "done": bool, "payload": dict}. When done is True the
    snooze has been applied and the caller should clear the pending row;
    otherwise the caller should persist the returned payload and keep waiting.
    """
    from app.openai_client import generate_snooze_resistance

    history = payload.get("history", [])
    history.append({"role": "user", "content": user_message})
    label = payload.get("label", "that")
    total_rounds = payload.get("total_rounds", 1)
    rounds_remaining = payload.get("rounds_remaining", 0)

    # One call both judges the reply and writes the response: if the push-back
    # worked (the user caved and will do it now) it returns a victory cheer with
    # conceded=True; otherwise an escalating push-back.
    round_num = total_rounds - rounds_remaining + 1
    result = generate_snooze_resistance(label, round_num, total_rounds, history)

    # The push-back worked: the user is doing the item now. Drop the snooze
    # entirely (the nag keeps its normal schedule) and cheer them on.
    if result["conceded"]:
        history.append({"role": "assistant", "content": result["reply"]})
        return {"reply": result["reply"], "done": True, "payload": payload}

    # Out of resistance and still not convinced — the bot relents and the
    # snooze finally lands.
    if rounds_remaining <= 0:
        concession = generate_snooze_resistance(
            label, total_rounds, total_rounds, history, relent=True
        )["reply"]
        confirm = execute_snooze(db, {
            "matched_id": payload["matched_id"],
            "matched_type": payload.get("matched_type", "nag"),
            "duration_minutes": payload.get("duration_minutes", 60),
        })
        return {"reply": f"{concession} {confirm}".strip(), "done": True, "payload": payload}

    # Still resisting — escalate.
    history.append({"role": "assistant", "content": result["reply"]})
    payload["history"] = history
    payload["rounds_remaining"] = rounds_remaining - 1
    return {"reply": result["reply"], "done": False, "payload": payload}


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
        nag.snooze_count = (nag.snooze_count or 0) + 1
        if nag.deadline_at:
            nag.deadline_at = nag.deadline_at + timedelta(minutes=duration)
        db.commit()
        log.info("Snoozed nag #%d for %d min: %s", nag.id, duration, nag.label)
        return f"Snoozed \"{nag.label}\" for {duration} min."

    return "Unknown item type."


def _capture_snooze_undo(db: Session, matched_id: int, matched_type: str) -> dict:
    """Capture current state before a snooze, for undo."""
    if matched_type == "nag":
        nag = db.query(NagSchedule).filter(NagSchedule.id == matched_id).first()
        if nag:
            return {
                "nag_id": nag.id,
                "prev_next_nag_at": nag.next_nag_at.isoformat() if nag.next_nag_at else None,
                "prev_deadline_at": nag.deadline_at.isoformat() if nag.deadline_at else None,
            }
    return {}


def undo_snooze(db: Session, payload: dict) -> str:
    """Reverse a snooze by restoring previous timing."""
    import logging
    log = logging.getLogger(__name__)
    undo = payload.get("undo_state", {})
    label = payload.get("label", "item")

    if "nag_id" in undo:
        nag = db.query(NagSchedule).filter(NagSchedule.id == undo["nag_id"]).first()
        if nag:
            if undo.get("prev_next_nag_at"):
                nag.next_nag_at = _parse_dt(undo["prev_next_nag_at"])
            if undo.get("prev_deadline_at"):
                nag.deadline_at = _parse_dt(undo["prev_deadline_at"])
            if nag.snooze_count:
                nag.snooze_count -= 1

    db.commit()
    log.info("Undid snooze for: %s", label)
    return f"Undone! \"{label}\" restored to its previous time."


def _handle_list(db: Session, data: dict) -> str:
    lines = []

    flashes = db.query(ScheduledFlash).filter(
        ScheduledFlash.user_phone == USER_PHONE,
        ScheduledFlash.status == "pending",
    ).order_by(ScheduledFlash.fire_at.asc()).all()
    if flashes:
        lines.append("LIGHT FLASHES:")
        for f in flashes:
            lines.append(f"  - {f.label or 'flash'} @ {_format_time(f.fire_at)}")

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
            elif n.deadline_offset_minutes:
                h, m = divmod(n.deadline_offset_minutes, 60)
                off = f"{h}h{m:02d}m" if h else f"{m}m"
                interval_desc = f" deadline +{off}/cycle"
            else:
                interval_desc = ""
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


def _handle_flash_lights(db: Session, data: dict) -> str:
    """Schedule a one-time basement-light flash at a specific time."""
    fire_at_str = data.get("fire_at")
    label = data.get("label") or "Light flash"
    if not fire_at_str:
        return "When should I flash the lights? Try \"flash lights at 9pm\"."
    try:
        fire_at = _parse_dt(fire_at_str)
    except (ValueError, TypeError):
        return "Couldn't read that time. Try \"flash lights at 9pm\"."

    flash = ScheduledFlash(
        user_phone=USER_PHONE,
        label=label,
        fire_at=fire_at,
        status="pending",
    )
    db.add(flash)
    db.commit()
    return f"Lights will flash at {_format_time(fire_at)}."


def _handle_help(db: Session, data: dict) -> str:
    return (
        "SMS ADHD Assistant commands:\n"
        "- Add to today list: \".. <thing>\" (then answer the deadline prompt)\n"
        "- Nag: \"nag me to enter my time at 9am every 15 min weekdays\"\n"
        "- Flash lights: \"flash lights at 9pm\"\n"
        "- Set your context: \"heading to Target\" / \"home for the night\"\n"
        "- Mark done: \"<thing> done\" or just \"done\"\n"
        "- Clear all: \"done all\"\n"
        "- Cancel: \"cancel [keyword]\" or \"nevermind\"\n"
        "- Snooze: \"snooze [keyword] [30]\" — you'll have to talk me into it\n"
        "- Morning briefing: \"briefing\" or \"what's my day look like\"\n"
        "- See your list: \"list\"\n"
        "- This message: \"commands\" or \"#help\"\n"
        "Prefix shortcuts:\n"
        "- #help — show this message (works around carrier blocking HELP/INFO)\n"
        "- #newlist <title> then items on new lines — create a checklist\n"
        "- #updatelist then items on new lines — append to current list\n"
        "- kk <message> — relay a message to Kathryn"
    )
