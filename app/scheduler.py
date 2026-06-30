"""Background scheduler — fires reminders, nags, and scheduled messages."""

import logging
import random
import threading
import time
import urllib.request
from datetime import datetime, timedelta, timezone

from app.database import SessionLocal
from app.models import NagSchedule, SmsLog, AppState, ProcessedEmail, ScheduledFlash
from app.config import (
    USER_PHONE, USER_TIMEZONE, TICK_SECONDS, GMAIL_SYNC_INTERVAL,
    BASEMENT_LIGHT_ON, BASEMENT_LIGHT_OFF, BRIEFING_TIME,
    QUIET_HOURS_START, QUIET_HOURS_END,
    DEFAULT_MIN_INTERVAL, GLOBAL_NAG_MIN_GAP, OVERDUE_PING_GAP,
    CALENDAR_IMPORT_TIME,
)
from app.twilio_client import send_sms
from app.intent_router import _next_cron_fire, _next_nag_cycle, roll_recurring_to_next_cycle
from app.morning_briefing import generate_morning_briefing, fetch_calendar_items

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)


def _call_webhook(url: str):
    """Fire a GET request to a webhook URL."""
    try:
        req = urllib.request.Request(url)
        with urllib.request.urlopen(req, timeout=10):
            pass
    except Exception:
        log.exception("Webhook call failed: %s", url)


def _flash_basement_light():
    """Flash basement light: off, on, off, on with 1-second gaps. Runs in a background thread."""
    if not BASEMENT_LIGHT_ON or not BASEMENT_LIGHT_OFF:
        log.warning("Basement light webhooks not configured, skipping flash")
        return

    def _do_flash():
        log.info("Flashing basement light")
        _call_webhook(BASEMENT_LIGHT_OFF)
        time.sleep(2)
        _call_webhook(BASEMENT_LIGHT_ON)
        time.sleep(2)
        _call_webhook(BASEMENT_LIGHT_OFF)
        time.sleep(2)
        _call_webhook(BASEMENT_LIGHT_ON)
        log.info("Basement light flash complete")

    threading.Thread(target=_do_flash, daemon=True).start()


def _log_outbound(db, body: str, sid: str):
    db.add(SmsLog(
        direction="outbound",
        phone=USER_PHONE,
        body=body,
        twilio_sid=sid,
    ))


def fire_due_flashes(db):
    """Trigger the basement-light flash for any scheduled flashes whose time has passed."""
    now = datetime.now(timezone.utc)
    flashes = db.query(ScheduledFlash).filter(
        ScheduledFlash.status == "pending",
        ScheduledFlash.fire_at <= now,
    ).with_for_update(skip_locked=True).all()

    for f in flashes:
        try:
            _flash_basement_light()
            f.status = "done"
            f.sent_at = now
            msg = f"Flashing the lights — {f.label}." if f.label else "Flashing the lights."
            result = send_sms(f.user_phone, msg)
            _log_outbound(db, msg, result.get("sid", ""))
            db.commit()
            log.info("Fired scheduled flash #%d: %s", f.id, f.label)
        except Exception:
            log.exception("Failed to fire flash #%d", f.id)
            db.rollback()


def _is_quiet_hours(tz_name):
    """Return True if the current local time is within quiet hours."""
    from zoneinfo import ZoneInfo
    local_now = datetime.now(ZoneInfo(tz_name))
    hour = local_now.hour
    if QUIET_HOURS_START < QUIET_HOURS_END:
        return QUIET_HOURS_START <= hour < QUIET_HOURS_END
    else:  # wraps midnight, e.g., 22 to 6
        return hour >= QUIET_HOURS_START or hour < QUIET_HOURS_END


def fire_cycle_starts(db):
    """Activate dormant nags whose scheduled start has arrived: set active_since
    and this cycle's deadline, then point next_nag_at at *now* so the first Zeno
    nag fires this same tick (via fire_due_nags). Sends nothing itself."""
    now = datetime.now(timezone.utc)
    nags = db.query(NagSchedule).filter(
        NagSchedule.status == "active",
        NagSchedule.active_since.is_(None),
        NagSchedule.next_nag_at <= now,
    ).with_for_update(skip_locked=True).all()

    for nag in nags:
        try:
            # Recurring missed-cycle skip: if this cycle's window is already over,
            # jump to the next cron fire without activating.
            if nag.repeating and nag.deadline_offset_minutes:
                window_end = nag.next_nag_at + timedelta(minutes=nag.deadline_offset_minutes)
                if now >= window_end:
                    nag.next_nag_at = _next_cron_fire(nag.cron_expression, nag.timezone)
                    db.commit()
                    log.info("Nag #%d skipped missed cycle: %s", nag.id, nag.label)
                    continue

            nag.active_since = now
            nag.nag_count = 0
            nag.snooze_count = 0
            nag.burst_armed = False
            if nag.repeating and nag.deadline_offset_minutes is not None:
                # Recurring nag: deadline is computed fresh each cycle.
                nag.deadline_at = now + timedelta(minutes=nag.deadline_offset_minutes)
            # Start the Zeno cadence now: fire_due_nags will pick this up and
            # advance next_nag_at by the Zeno interval after each send. The next
            # cron cycle is recomputed fresh on check-off / overnight rollover.
            nag.next_nag_at = now
            db.commit()
            log.info("Nag #%d cycle started: %s", nag.id, nag.label)
        except Exception:
            log.exception("Failed to start cycle for nag #%d", nag.id)
            db.rollback()


def _compute_deadline_interval(nag, now):
    """Zeno's-paradox cadence toward the expire time.

    Each step waits a random fraction (0.25–0.5) of the time remaining until
    deadline_at, so nags accelerate as the deadline nears (with jitter rather
    than a fixed curve), clamped to the item's min_interval_minutes or the global
    DEFAULT_MIN_INTERVAL floor. Once the deadline has passed, acceleration stops
    and the item simply pings every OVERDUE_PING_GAP minutes until done/snoozed.
    No deadline → flat min interval."""
    min_iv = nag.min_interval_minutes or DEFAULT_MIN_INTERVAL
    if nag.deadline_at and now >= nag.deadline_at:
        return OVERDUE_PING_GAP
    if not nag.deadline_at:
        return min_iv
    remaining_minutes = (nag.deadline_at - now).total_seconds() / 60.0
    fraction = random.uniform(0.25, 0.5)
    interval = remaining_minutes * fraction
    return max(min_iv, int(round(interval)))


def _single_nag_message(nag, now) -> str:
    """Build the urgency-tailored message for a single due nag."""
    nag_num = nag.nag_count + 1
    if nag.deadline_at:
        try:
            from app.openai_client import generate_deadline_nag_message
            msg = generate_deadline_nag_message(nag, now)
        except Exception:
            log.exception("GPT deadline message failed for nag #%d, using fallback", nag.id)
            msg = f"(#{nag_num}) {nag.label} — deadline approaching!\nReply DONE when finished."
    else:
        msg = nag.message
        if nag_num > 1:
            msg = f"(#{nag_num}) {msg}"
    if not msg.rstrip().endswith("Reply DONE when finished."):
        msg += "\nReply DONE when finished."
    return msg


def _combined_nag_message(nags, now) -> str:
    """Coalesce several simultaneously-due nags into one SMS: a numbered list
    plus a short GPT-written plan line at the bottom (omitted on GPT failure)."""
    lines = ["Due now:"]
    for i, nag in enumerate(nags, 1):
        lines.append(f"{i}) {nag.label}")
    body = "\n".join(lines)
    try:
        from app.openai_client import generate_nag_plan
        plan = generate_nag_plan([n.label for n in nags])
    except Exception:
        log.exception("GPT nag plan failed, omitting plan line")
        plan = ""
    if plan:
        body += f"\n\n{plan}"
    body += "\n\nReply DONE <item> when finished."
    return body


def fire_due_nags(db):
    """Zeno cadence — the single send loop for active items.

    For every active item whose next_nag_at has arrived: prepare it (skip if
    checked off, defer if quiet hours), then — subject to the global rate gate —
    send one (possibly coalesced) nag SMS and advance each item's next_nag_at by
    its Zeno interval (accelerating toward the expire time, or a flat
    OVERDUE_PING_GAP once overdue)."""
    from app.context_engine import is_done_today
    now = datetime.now(timezone.utc)
    nags = db.query(NagSchedule).filter(
        NagSchedule.status == "active",
        NagSchedule.active_since.isnot(None),
        NagSchedule.next_nag_at <= now,
    ).with_for_update(skip_locked=True).all()

    ready = []
    for nag in nags:
        try:
            # Checked off earlier today but lingering for display — stop nagging.
            if is_done_today(nag, now):
                continue

            # Quiet hours: defer this item's next nag to when quiet hours end.
            if _is_quiet_hours(nag.timezone):
                from zoneinfo import ZoneInfo
                local_now = datetime.now(ZoneInfo(nag.timezone))
                resume_at = local_now.replace(hour=QUIET_HOURS_END, minute=0, second=0, microsecond=0)
                if resume_at <= local_now:
                    resume_at += timedelta(days=1)
                nag.next_nag_at = resume_at.astimezone(timezone.utc)
                db.commit()
                log.info("Nag #%d deferred to %s (quiet hours): %s", nag.id, resume_at, nag.label)
                continue

            ready.append(nag)
        except Exception:
            log.exception("Failed to prepare nag #%d", nag.id)
            db.rollback()

    if not ready:
        return

    # Global rate gate: at most one nag SMS per GLOBAL_NAG_MIN_GAP window.
    last = _get_state(db, "last_nag_sent_at")
    if last:
        try:
            if now - datetime.fromisoformat(last) < timedelta(minutes=GLOBAL_NAG_MIN_GAP):
                log.info("Nag send gated: %d due, <%dm since last send", len(ready), GLOBAL_NAG_MIN_GAP)
                return
        except ValueError:
            pass

    # Send one (possibly coalesced) message for everything due this window.
    try:
        if len(ready) == 1:
            msg = _single_nag_message(ready[0], now)
        else:
            msg = _combined_nag_message(ready, now)

        result = send_sms(USER_PHONE, msg)
        for nag in ready:
            nag.nag_count += 1
            interval = _compute_deadline_interval(nag, now)
            nag.next_nag_at = now + timedelta(minutes=interval)
        _set_state(db, "last_nag_sent_at", now.isoformat())
        _log_outbound(db, msg, result.get("sid", ""))
        db.commit()
        log.info("Fired %d nag(s): %s", len(ready), ", ".join(n.label for n in ready))
    except Exception:
        log.exception("Failed to send coalesced nag batch")
        db.rollback()


def _get_state(db, key: str) -> str | None:
    row = db.query(AppState).filter(AppState.key == key).first()
    return row.value if row else None


def _set_state(db, key: str, value: str):
    row = db.query(AppState).filter(AppState.key == key).first()
    if row:
        row.value = value
        row.updated_at = datetime.now(timezone.utc)
    else:
        db.add(AppState(key=key, value=value))


def _rollover_missed(db, now):
    """Find open items whose expire time was before today and were never checked
    off. Reset recurring ones to dormant (so today gets a fresh cycle) and carry
    one-shots onto today (re-dated to 11 PM). Returns the missed labels; the
    caller commits."""
    from zoneinfo import ZoneInfo
    from app.context_engine import cycle_deadline, is_done_today
    tz = ZoneInfo(USER_TIMEZONE)
    start_today = now.astimezone(tz).replace(hour=0, minute=0, second=0, microsecond=0)

    missed = []
    nags = db.query(NagSchedule).filter(NagSchedule.status == "active").all()
    for nag in nags:
        if is_done_today(nag, now):
            continue
        dl = cycle_deadline(nag, now)
        if dl is None or dl >= start_today:
            continue  # no deadline, or expires today/later — not a miss
        missed.append(nag.label)
        nag.burst_armed = False
        if nag.repeating:
            # Roll onto the next legitimate cycle (dormant if its cron fires
            # later today; carried-active pinned to 11 PM if it already fired).
            roll_recurring_to_next_cycle(nag, now)
        else:
            # Carry the one-shot onto today, re-dated to 11 PM local.
            eod = now.astimezone(tz).replace(hour=23, minute=0, second=0, microsecond=0)
            nag.deadline_at = eod.astimezone(timezone.utc)
    return missed


def fire_morning_briefing(db):
    """Send the morning briefing once/day past BRIEFING_TIME, plus the overnight
    rollover: surface items missed the previous day in a single recap SMS and
    reset their cycles so today's Zeno cadence picks them up."""
    try:
        from zoneinfo import ZoneInfo
        now_local = datetime.now(ZoneInfo(USER_TIMEZONE))
    except Exception:
        now_local = datetime.now()

    today = now_local.date()
    today_str = today.isoformat()

    last_sent = _get_state(db, "briefing_last_sent_date")
    if last_sent == today_str:
        return

    # Parse configured briefing time
    try:
        hour, minute = map(int, BRIEFING_TIME.split(":"))
    except (ValueError, AttributeError):
        hour, minute = 7, 30

    if now_local.hour < hour or (now_local.hour == hour and now_local.minute < minute):
        return

    now = datetime.now(timezone.utc)
    try:
        missed = _rollover_missed(db, now)
        msg = generate_morning_briefing()
        result = send_sms(USER_PHONE, msg)
        _log_outbound(db, msg, result.get("sid", ""))
        if missed:
            joined = ", ".join(f'"{m}"' for m in missed)
            recap = (f"MISSED yesterday: {joined}. Carried to today — "
                     f"reply DONE <item> when handled.")
            recap_result = send_sms(USER_PHONE, recap)
            _log_outbound(db, recap, recap_result.get("sid", ""))
            log.info("Rolled over %d missed item(s): %s", len(missed), ", ".join(missed))
        _set_state(db, "briefing_last_sent_date", today_str)
        db.commit()
        log.info("Morning briefing sent")
    except Exception:
        log.exception("Failed to send morning briefing")
        db.rollback()


def _past_daily_time(now_local, hhmm: str, default=(8, 0)) -> bool:
    """True if local time is at/after the configured HH:MM for today."""
    try:
        hour, minute = map(int, hhmm.split(":"))
    except (ValueError, AttributeError):
        hour, minute = default
    return now_local.hour > hour or (now_local.hour == hour and now_local.minute >= minute)


def fire_calendar_import(db):
    """Once/day past CALENDAR_IMPORT_TIME, add today's calendar events to the
    today list as one-shot items. Timed events expire at their start time;
    all-day events expire at 11 PM. Deduped per event per day via source_ref."""
    try:
        from zoneinfo import ZoneInfo
        now_local = datetime.now(ZoneInfo(USER_TIMEZONE))
    except Exception:
        now_local = datetime.now()

    today_str = now_local.date().isoformat()
    if _get_state(db, "calendar_last_imported_date") == today_str:
        return
    if not _past_daily_time(now_local, CALENDAR_IMPORT_TIME):
        return

    now = datetime.now(timezone.utc)
    try:
        events = fetch_calendar_items()
    except Exception:
        log.exception("Calendar import: failed to fetch events")
        return

    from zoneinfo import ZoneInfo
    tz = ZoneInfo(USER_TIMEZONE)
    eod = now.astimezone(tz).replace(hour=23, minute=0, second=0, microsecond=0).astimezone(timezone.utc)

    added = 0
    try:
        for ev in events:
            source_ref = f"cal:{ev['uid']}:{today_str}"
            exists = db.query(NagSchedule).filter(
                NagSchedule.source == "calendar",
                NagSchedule.source_ref == source_ref,
            ).first()
            if exists:
                continue
            deadline_at = ev["start"] if not ev["all_day"] and ev["start"] else eod
            db.add(NagSchedule(
                user_phone=USER_PHONE,
                label=ev["summary"],
                message=f"Calendar: {ev['summary']}",
                timezone=USER_TIMEZONE,
                next_nag_at=now,
                active_since=now,       # on the list immediately, one-shot
                nag_count=0,
                repeating=False,
                deadline_at=deadline_at,
                source="calendar",
                source_ref=source_ref,
                status="active",
            ))
            added += 1
        _set_state(db, "calendar_last_imported_date", today_str)
        db.commit()
        log.info("Calendar import: added %d event(s) to today's list", added)
    except Exception:
        log.exception("Calendar import: failed to create nags")
        db.rollback()


def run_gmail_sync():
    """Run Gmail sync if the module is available."""
    try:
        from app.gmail_sync import sync_gmail_action_items
        sync_gmail_action_items()
    except Exception:
        log.exception("Gmail sync failed")


def main():
    """Main scheduler loop."""
    log.info("Starting scheduler (tick=%ds, gmail_sync=%ds)", TICK_SECONDS, GMAIL_SYNC_INTERVAL)

    from app.migrations import run_migrations
    run_migrations()

    # Send recovery notification
    try:
        from zoneinfo import ZoneInfo
        now_local = datetime.now(ZoneInfo(USER_TIMEZONE))
    except Exception:
        now_local = datetime.now()
    msg = f"ADHD Bot back online — scheduler recovered at {now_local.strftime('%-I:%M %p on %b %d')}."
    try:
        result = send_sms(USER_PHONE, msg)
        db = SessionLocal()
        try:
            _log_outbound(db, msg, result.get("sid", ""))
            db.commit()
        finally:
            db.close()
        log.info("Recovery notification sent")
    except Exception:
        log.exception("Failed to send recovery notification")

    last_gmail_sync = 0.0

    while True:
        db = SessionLocal()
        try:
            fire_morning_briefing(db)
            fire_calendar_import(db)  # add today's calendar events to the list at 8am
            fire_due_flashes(db)
            # Context-aware surfacing runs only when the user sends a context SMS
            # (handled in _handle_context_update), not on a timer.
            fire_cycle_starts(db)   # activate dormant nags at their start time
            fire_due_nags(db)       # Zeno cadence: send accelerating per-item nags
        except Exception:
            log.exception("Scheduler tick error")
        finally:
            db.close()

        # Gmail sync on a longer interval
        now_ts = time.time()
        if now_ts - last_gmail_sync >= GMAIL_SYNC_INTERVAL:
            run_gmail_sync()
            last_gmail_sync = now_ts

        time.sleep(TICK_SECONDS)


if __name__ == "__main__":
    main()
