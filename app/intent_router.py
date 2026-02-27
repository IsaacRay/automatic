"""Map parsed intents to DB operations and reply text."""

import uuid
from datetime import datetime, timedelta, timezone

from sqlalchemy.orm import Session

from app.models import Reminder, RecurringSchedule, ActionItem
from app.config import USER_PHONE, USER_TIMEZONE


def _parse_dt(s: str) -> datetime:
    """Parse an ISO 8601 datetime string, ensuring it's timezone-aware UTC."""
    dt = datetime.fromisoformat(s)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def _now_local():
    from zoneinfo import ZoneInfo
    return datetime.now(ZoneInfo(USER_TIMEZONE))


def _format_time(dt: datetime) -> str:
    """Format a UTC datetime into a human-readable local time string."""
    from zoneinfo import ZoneInfo
    local = dt.astimezone(ZoneInfo(USER_TIMEZONE))
    return local.strftime("%a %b %d %I:%M %p")


def _next_cron_fire(cron_expr: str, tz_name: str) -> datetime:
    """Compute the next fire time for a cron expression, returned as UTC."""
    from zoneinfo import ZoneInfo
    from croniter import croniter
    local_now = datetime.now(ZoneInfo(tz_name))
    cron = croniter(cron_expr, local_now)
    next_local = cron.get_next(datetime)
    return next_local.astimezone(timezone.utc)


def _backoff_delta(remind_count: int) -> timedelta:
    """Compute the backoff delay for action item re-reminders."""
    if remind_count == 0:
        return timedelta(hours=0)  # immediate
    elif remind_count == 1:
        return timedelta(hours=4)
    elif remind_count == 2:
        return timedelta(hours=24)
    else:
        return timedelta(hours=48)


def handle_intent(db: Session, parsed: dict) -> str:
    """Dispatch a parsed intent to the appropriate handler. Returns reply text."""
    intent = parsed.get("intent", "unknown")
    data = parsed.get("data", {})

    handlers = {
        "create_reminder": _handle_create_reminder,
        "create_recurring": _handle_create_recurring,
        "acknowledge": _handle_acknowledge,
        "cancel": _handle_cancel,
        "snooze": _handle_snooze,
        "list": _handle_list,
        "help": _handle_help,
    }
    handler = handlers.get(intent)
    if handler:
        return handler(db, data)
    return "I didn't understand that. Text HELP to see what I can do."


def _handle_create_reminder(db: Session, data: dict) -> str:
    label = data.get("label", "Reminder")
    reminders_data = data.get("reminders", [])
    parent_event_id = data.get("parent_event_id") or f"evt_{uuid.uuid4().hex[:12]}"

    if not reminders_data:
        return "I couldn't figure out when to remind you. Try again with a time?"

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


def _handle_create_recurring(db: Session, data: dict) -> str:
    label = data.get("label", "Recurring message")
    cron_expr = data.get("cron_expression", "")
    message_prompt = data.get("message_prompt", label)

    if not cron_expr:
        return "I couldn't figure out the schedule. Try something like 'every day at 5pm'?"

    next_fire = _next_cron_fire(cron_expr, USER_TIMEZONE)
    schedule = RecurringSchedule(
        user_phone=USER_PHONE,
        label=label,
        message_prompt=message_prompt,
        cron_expression=cron_expr,
        timezone=USER_TIMEZONE,
        next_fire_at=next_fire,
        status="active",
    )
    db.add(schedule)
    db.commit()

    return f"Recurring schedule created: \"{label}\" (cron: {cron_expr}). Next: {_format_time(next_fire)}"


def _handle_acknowledge(db: Session, data: dict) -> str:
    keyword = data.get("keyword")
    ack_all = data.get("all", False)
    now = datetime.now(timezone.utc)

    if ack_all:
        # Mark all pending reminders as dismissed
        reminders = db.query(Reminder).filter(
            Reminder.user_phone == USER_PHONE,
            Reminder.status.in_(["pending", "sent"]),
        ).all()
        for r in reminders:
            r.status = "dismissed"

        # Mark all pending action items as done
        items = db.query(ActionItem).filter(
            ActionItem.user_phone == USER_PHONE,
            ActionItem.status == "pending",
        ).all()
        for item in items:
            item.status = "done"
            item.completed_at = now

        db.commit()
        total = len(reminders) + len(items)
        return f"Cleared all! Marked {total} items as done/dismissed."

    # Try to match by keyword
    if keyword:
        # Check reminders
        reminder = db.query(Reminder).filter(
            Reminder.user_phone == USER_PHONE,
            Reminder.status.in_(["pending", "sent"]),
            Reminder.label.ilike(f"%{keyword}%"),
        ).order_by(Reminder.created_at.desc()).first()

        if reminder:
            # Dismiss all siblings with same parent_event_id
            if reminder.parent_event_id:
                siblings = db.query(Reminder).filter(
                    Reminder.parent_event_id == reminder.parent_event_id,
                    Reminder.status.in_(["pending", "sent"]),
                ).all()
                for s in siblings:
                    s.status = "dismissed"
            else:
                reminder.status = "dismissed"
            db.commit()
            return f"Dismissed: \"{reminder.label}\""

        # Check action items
        item = db.query(ActionItem).filter(
            ActionItem.user_phone == USER_PHONE,
            ActionItem.status == "pending",
            ActionItem.description.ilike(f"%{keyword}%"),
        ).order_by(ActionItem.created_at.desc()).first()

        if item:
            item.status = "done"
            item.completed_at = now
            db.commit()
            return f"Done: \"{item.description}\""

        return f"Couldn't find anything matching \"{keyword}\". Text LIST to see your items."

    # No keyword — mark most recent sent reminder or pending action item
    reminder = db.query(Reminder).filter(
        Reminder.user_phone == USER_PHONE,
        Reminder.status == "sent",
    ).order_by(Reminder.sent_at.desc()).first()

    if reminder:
        if reminder.parent_event_id:
            siblings = db.query(Reminder).filter(
                Reminder.parent_event_id == reminder.parent_event_id,
                Reminder.status.in_(["pending", "sent"]),
            ).all()
            for s in siblings:
                s.status = "dismissed"
        else:
            reminder.status = "dismissed"
        db.commit()
        return f"Dismissed: \"{reminder.label}\""

    item = db.query(ActionItem).filter(
        ActionItem.user_phone == USER_PHONE,
        ActionItem.status == "pending",
    ).order_by(ActionItem.next_remind_at.asc()).first()

    if item:
        item.status = "done"
        item.completed_at = now
        db.commit()
        return f"Done: \"{item.description}\""

    return "Nothing pending to mark as done!"


def _handle_cancel(db: Session, data: dict) -> str:
    keyword = data.get("keyword")
    target_type = data.get("type")  # "reminder", "recurring", or "action"

    # --- Try reminders ---
    if target_type in (None, "reminder"):
        rq = db.query(Reminder).filter(
            Reminder.user_phone == USER_PHONE,
            Reminder.status.in_(["pending", "sent"]),
        )
        if keyword:
            rq = rq.filter(Reminder.label.ilike(f"%{keyword}%"))
        reminder = rq.order_by(Reminder.created_at.desc()).first()

        if reminder:
            # Cancel all siblings with same parent_event_id
            if reminder.parent_event_id:
                siblings = db.query(Reminder).filter(
                    Reminder.parent_event_id == reminder.parent_event_id,
                    Reminder.status.in_(["pending", "sent"]),
                ).all()
                for s in siblings:
                    s.status = "cancelled"
            else:
                reminder.status = "cancelled"
            db.commit()
            return f"Cancelled: \"{reminder.label}\""

    # --- Try recurring schedules ---
    if target_type in (None, "recurring"):
        sq = db.query(RecurringSchedule).filter(
            RecurringSchedule.user_phone == USER_PHONE,
            RecurringSchedule.status == "active",
        )
        if keyword:
            sq = sq.filter(RecurringSchedule.label.ilike(f"%{keyword}%"))
        schedule = sq.order_by(RecurringSchedule.created_at.desc()).first()

        if schedule:
            schedule.status = "deleted"
            db.commit()
            return f"Cancelled recurring: \"{schedule.label}\""

    # --- Try action items ---
    if target_type in (None, "action"):
        aq = db.query(ActionItem).filter(
            ActionItem.user_phone == USER_PHONE,
            ActionItem.status == "pending",
        )
        if keyword:
            aq = aq.filter(ActionItem.description.ilike(f"%{keyword}%"))
        item = aq.order_by(ActionItem.created_at.desc()).first()

        if item:
            item.status = "done"
            item.completed_at = datetime.now(timezone.utc)
            db.commit()
            return f"Cancelled: \"{item.description}\""

    if keyword:
        return f"Couldn't find anything matching \"{keyword}\". Text LIST to see your items."
    return "Nothing to cancel!"


def _handle_snooze(db: Session, data: dict) -> str:
    duration = data.get("duration_minutes", 60)
    keyword = data.get("keyword")
    now = datetime.now(timezone.utc)
    snooze_until = now + timedelta(minutes=duration)

    query = db.query(ActionItem).filter(
        ActionItem.user_phone == USER_PHONE,
        ActionItem.status == "pending",
    )
    if keyword:
        query = query.filter(ActionItem.description.ilike(f"%{keyword}%"))

    item = query.order_by(ActionItem.next_remind_at.asc()).first()
    if item:
        item.snooze_until = snooze_until
        item.next_remind_at = snooze_until
        db.commit()
        return f"Snoozed \"{item.description}\" for {duration} minutes."

    # Try reminders
    rq = db.query(Reminder).filter(
        Reminder.user_phone == USER_PHONE,
        Reminder.status.in_(["pending", "sent"]),
    )
    if keyword:
        rq = rq.filter(Reminder.label.ilike(f"%{keyword}%"))

    reminder = rq.order_by(Reminder.fire_at.asc()).first()
    if reminder:
        reminder.fire_at = snooze_until
        reminder.status = "pending"
        db.commit()
        return f"Snoozed \"{reminder.label}\" for {duration} minutes."

    return "Nothing to snooze!"


def _handle_list(db: Session, data: dict) -> str:
    lines = []

    reminders = db.query(Reminder).filter(
        Reminder.user_phone == USER_PHONE,
        Reminder.status.in_(["pending", "sent"]),
    ).order_by(Reminder.fire_at.asc()).all()
    if reminders:
        lines.append("REMINDERS:")
        for r in reminders:
            lines.append(f"  - {r.label} @ {_format_time(r.fire_at)} [{r.status}]")

    items = db.query(ActionItem).filter(
        ActionItem.user_phone == USER_PHONE,
        ActionItem.status == "pending",
    ).order_by(ActionItem.next_remind_at.asc()).all()
    if items:
        lines.append("ACTION ITEMS:")
        for item in items:
            src = f" ({item.source})" if item.source else ""
            lines.append(f"  - {item.description}{src}")

    recurring = db.query(RecurringSchedule).filter(
        RecurringSchedule.user_phone == USER_PHONE,
        RecurringSchedule.status == "active",
    ).order_by(RecurringSchedule.next_fire_at.asc()).all()
    if recurring:
        lines.append("RECURRING:")
        for s in recurring:
            lines.append(f"  - {s.label} (next: {_format_time(s.next_fire_at)})")

    if not lines:
        return "All clear! Nothing pending."

    return "\n".join(lines)


def _handle_help(db: Session, data: dict) -> str:
    return (
        "SMS ADHD Assistant commands:\n"
        "- Set a reminder: \"meeting at 4pm friday about X\"\n"
        "- Recurring: \"remind me to exercise every day at 5pm\"\n"
        "- Mark done: \"done\" or \"done [keyword]\"\n"
        "- Clear all: \"done all\"\n"
        "- Cancel: \"cancel [keyword]\" or \"nevermind\"\n"
        "- Snooze: \"snooze\" or \"snooze 30\" (minutes)\n"
        "- See pending: \"list\"\n"
        "- This message: \"help\""
    )
