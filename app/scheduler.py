"""Background scheduler — fires reminders, nags, and scheduled messages."""

import logging
import threading
import time
import urllib.request
from datetime import datetime, timedelta, timezone

from app.database import SessionLocal
from app.models import Reminder, NagSchedule, SmsLog, AppState, ProcessedEmail
from app.config import (
    USER_PHONE, USER_TIMEZONE, TICK_SECONDS, GMAIL_SYNC_INTERVAL,
    BASEMENT_LIGHT_ON, BASEMENT_LIGHT_OFF, BRIEFING_TIME,
    EXERCISE_MORNING_TIME, EXERCISE_EVENING_TIME,
    QUIET_HOURS_START, QUIET_HOURS_END, DEFAULT_MIN_INTERVAL, DEFAULT_MAX_INTERVAL,
)
from app.twilio_client import send_sms
from app.intent_router import _next_cron_fire, _next_nag_cycle
from app.morning_briefing import generate_morning_briefing
from app.exercise_motivation import generate_exercise_morning_message, generate_exercise_evening_message

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


def _is_event_time_reminder(db, reminder: "Reminder") -> bool:
    """Return True if this reminder should trigger a light flash.

    Flashes for any event reminder (has parent_event_id) unless it's the
    prep reminder (an earlier reminder in a pair that has a later sibling).
    """
    if not reminder.parent_event_id:
        return False
    later = db.query(Reminder).filter(
        Reminder.parent_event_id == reminder.parent_event_id,
        Reminder.fire_at > reminder.fire_at,
    ).first()
    return later is None


def _log_outbound(db, body: str, sid: str):
    db.add(SmsLog(
        direction="outbound",
        phone=USER_PHONE,
        body=body,
        twilio_sid=sid,
    ))


def fire_due_reminders(db):
    """Send SMS for all reminders whose fire_at has passed. Recurring reminders reschedule themselves."""
    now = datetime.now(timezone.utc)
    reminders = db.query(Reminder).filter(
        Reminder.status == "pending",
        Reminder.fire_at <= now,
    ).with_for_update(skip_locked=True).all()

    for r in reminders:
        try:
            result = send_sms(r.user_phone, r.message)
            r.sent_at = now
            _log_outbound(db, r.message, result.get("sid", ""))

            if r.cron_expression:
                # Recurring reminder — reschedule for next fire
                tz = r.timezone or USER_TIMEZONE
                r.fire_at = _next_cron_fire(r.cron_expression, tz)
                r.status = "pending"
                log.info("Fired recurring reminder #%d: %s (next: %s)", r.id, r.label, r.fire_at)
            else:
                # One-shot reminder
                r.status = "sent"
                log.info("Fired reminder #%d: %s", r.id, r.label)
                if _is_event_time_reminder(db, r):
                    _flash_basement_light()

            db.commit()
        except Exception:
            log.exception("Failed to fire reminder #%d", r.id)
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


def _compute_deadline_interval(nag, now):
    """Compute dynamic interval using Zeno's paradox: interval = 1/3 of remaining time.

    9 days out → wait 3 days → wait 2 days → wait ~1.3 days → ... → clamp to min_interval.
    Past deadline: clamp to min_interval.
    """
    min_iv = nag.min_interval_minutes or DEFAULT_MIN_INTERVAL

    if not nag.deadline_at or now >= nag.deadline_at:
        return min_iv

    remaining_minutes = (nag.deadline_at - now).total_seconds() / 60.0
    interval = remaining_minutes / 3.0
    return max(min_iv, int(round(interval)))


def fire_due_nags(db):
    """Process nag schedules: start cycles, send nags, end expired cycles."""
    now = datetime.now(timezone.utc)
    nags = db.query(NagSchedule).filter(
        NagSchedule.status == "active",
        NagSchedule.next_nag_at <= now,
    ).with_for_update(skip_locked=True).all()

    for nag in nags:
        try:
            # Case 1: New cycle starting (dormant → active)
            if nag.active_since is None:
                # Recurring nag missed-cycle skip: if cycle_start + offset is already past, jump to next
                if nag.repeating and nag.deadline_offset_minutes:
                    window_end = nag.next_nag_at + timedelta(minutes=nag.deadline_offset_minutes)
                    if now >= window_end:
                        nag.next_nag_at = _next_cron_fire(nag.cron_expression, nag.timezone)
                        db.commit()
                        log.info("Nag #%d skipped missed cycle: %s", nag.id, nag.label)
                        continue

                nag.active_since = now
                nag.nag_count = 0
                if nag.repeating and nag.deadline_offset_minutes is not None:
                    # Recurring nag: deadline is computed fresh each cycle
                    nag.deadline_at = now + timedelta(minutes=nag.deadline_offset_minutes)
                # One-shot nag: deadline_at was set at creation — leave it alone

            # Quiet hours gate — applies to all nags
            if _is_quiet_hours(nag.timezone):
                from zoneinfo import ZoneInfo
                tz = ZoneInfo(nag.timezone)
                local_now = datetime.now(tz)
                resume_at = local_now.replace(hour=QUIET_HOURS_END, minute=0, second=0, microsecond=0)
                if resume_at <= local_now:
                    resume_at += timedelta(days=1)
                nag.next_nag_at = resume_at.astimezone(timezone.utc)
                db.commit()
                log.info("Nag #%d deferred to %s (quiet hours): %s", nag.id, resume_at, nag.label)
                continue

            # Case 2: Send nag (always uses Zeno curve off deadline_at; clamps to min past deadline)
            nag_num = nag.nag_count + 1

            if nag.deadline_at:
                try:
                    from app.openai_client import generate_deadline_nag_message
                    msg = generate_deadline_nag_message(nag, now)
                except Exception:
                    log.exception("GPT deadline message failed for nag #%d, using fallback", nag.id)
                    msg = f"(#{nag_num}) {nag.label} — deadline approaching!\nReply DONE when finished."
            else:
                # Safety fallback: shouldn't happen post-refactor, but handle it anyway
                msg = nag.message
                if nag_num > 1:
                    msg = f"(#{nag_num}) {msg}"

            if not msg.rstrip().endswith("Reply DONE when finished."):
                msg += "\nReply DONE when finished."

            result = send_sms(nag.user_phone, msg)
            nag.nag_count = nag_num
            interval = _compute_deadline_interval(nag, now)
            nag.next_nag_at = now + timedelta(minutes=interval)
            _log_outbound(db, msg, result.get("sid", ""))
            db.commit()
            log.info("Fired nag #%d (count=%d, interval=%dm): %s", nag.id, nag_num, interval, nag.label)

        except Exception:
            log.exception("Failed to process nag #%d", nag.id)
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


def fire_morning_briefing(db):
    """Send the morning briefing SMS if it's past BRIEFING_TIME and hasn't been sent today."""
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

    try:
        msg = generate_morning_briefing()
        result = send_sms(USER_PHONE, msg)
        _log_outbound(db, msg, result.get("sid", ""))
        _set_state(db, "briefing_last_sent_date", today_str)
        db.commit()
        log.info("Morning briefing sent")
    except Exception:
        log.exception("Failed to send morning briefing")
        db.rollback()


def fire_exercise_morning(db):
    """Send the exercise morning motivation SMS if it's past EXERCISE_MORNING_TIME and hasn't been sent today."""
    try:
        from zoneinfo import ZoneInfo
        now_local = datetime.now(ZoneInfo(USER_TIMEZONE))
    except Exception:
        now_local = datetime.now()

    today_str = now_local.date().isoformat()

    last_sent = _get_state(db, "exercise_morning_last_sent_date")
    if last_sent == today_str:
        return

    try:
        hour, minute = map(int, EXERCISE_MORNING_TIME.split(":"))
    except (ValueError, AttributeError):
        hour, minute = 10, 0

    if now_local.hour < hour or (now_local.hour == hour and now_local.minute < minute):
        return

    try:
        msg = generate_exercise_morning_message()
        result = send_sms(USER_PHONE, msg)
        _log_outbound(db, msg, result.get("sid", ""))
        _set_state(db, "exercise_morning_last_sent_date", today_str)
        db.commit()
        log.info("Exercise morning motivation sent")
    except Exception:
        log.exception("Failed to send exercise morning motivation")
        db.rollback()


def fire_exercise_evening(db):
    """Send the exercise evening recommendation SMS if it's past EXERCISE_EVENING_TIME and hasn't been sent today."""
    try:
        from zoneinfo import ZoneInfo
        now_local = datetime.now(ZoneInfo(USER_TIMEZONE))
    except Exception:
        now_local = datetime.now()

    today_str = now_local.date().isoformat()

    last_sent = _get_state(db, "exercise_evening_last_sent_date")
    if last_sent == today_str:
        return

    try:
        hour, minute = map(int, EXERCISE_EVENING_TIME.split(":"))
    except (ValueError, AttributeError):
        hour, minute = 17, 0

    if now_local.hour < hour or (now_local.hour == hour and now_local.minute < minute):
        return

    try:
        msg = generate_exercise_evening_message()
        result = send_sms(USER_PHONE, msg)
        _log_outbound(db, msg, result.get("sid", ""))
        _set_state(db, "exercise_evening_last_sent_date", today_str)
        db.commit()
        log.info("Exercise evening recommendation sent")
    except Exception:
        log.exception("Failed to send exercise evening recommendation")
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
            fire_exercise_morning(db)
            fire_exercise_evening(db)
            fire_due_reminders(db)
            fire_due_nags(db)
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
