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
    QUIET_HOURS_START, QUIET_HOURS_END, DEFAULT_MIN_INTERVAL, DEFAULT_MAX_INTERVAL,
    GLOBAL_NAG_MIN_GAP,
)
from app.twilio_client import send_sms
from app.intent_router import _next_cron_fire, _next_nag_cycle
from app.morning_briefing import generate_morning_briefing

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


def _compute_deadline_interval(nag, now):
    """Compute dynamic interval using Zeno's paradox.

    Each step waits a random fraction (0.25–0.5) of the remaining time, so the
    cadence accelerates toward the deadline but jitters rather than following a
    fixed curve. Clamped to min_interval (which never drops below the global
    5-minute floor enforced in fire_due_nags). Past deadline: clamp to min.
    """
    min_iv = nag.min_interval_minutes or DEFAULT_MIN_INTERVAL

    if not nag.deadline_at or now >= nag.deadline_at:
        return min_iv

    remaining_minutes = (nag.deadline_at - now).total_seconds() / 60.0
    fraction = random.uniform(0.25, 0.5)
    interval = remaining_minutes * fraction
    return max(min_iv, int(round(interval)))


def _single_nag_message(nag, now) -> str:
    """Build the rich, urgency-tailored message for a single due nag."""
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
    """Process nag schedules: start cycles, then send at most one (possibly
    coalesced) nag SMS per global 5-minute window.

    Per tick: prepare every due nag (cycle-start state, quiet-hours deferral),
    collect those ready to send, then — if the global gate is open — send a
    single message (rich for one item, numbered list + plan for several) and
    advance each included nag's Zeno interval.
    """
    now = datetime.now(timezone.utc)
    nags = db.query(NagSchedule).filter(
        NagSchedule.status == "active",
        NagSchedule.next_nag_at <= now,
    ).with_for_update(skip_locked=True).all()

    ready = []
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
                nag.snooze_count = 0
                if nag.repeating and nag.deadline_offset_minutes is not None:
                    # Recurring nag: deadline is computed fresh each cycle
                    nag.deadline_at = now + timedelta(minutes=nag.deadline_offset_minutes)
                    # Don't fire at the exact cron time — defer the first nag by a
                    # random Zeno step so daily items land at varied times through
                    # the day rather than all bursting at cycle start.
                    interval = _compute_deadline_interval(nag, now)
                    nag.next_nag_at = now + timedelta(minutes=interval)
                    db.commit()
                    log.info("Nag #%d cycle start, first nag in %dm: %s", nag.id, interval, nag.label)
                    continue
                # One-shot nag: deadline_at was set at creation — leave it alone
                db.commit()

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
            last_dt = datetime.fromisoformat(last)
            if now - last_dt < timedelta(minutes=GLOBAL_NAG_MIN_GAP):
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
    last_context_eval = 0.0
    CONTEXT_EVAL_INTERVAL = 600  # ~10 min: surface context-relevant items even without a new context SMS

    while True:
        db = SessionLocal()
        try:
            fire_morning_briefing(db)
            fire_due_flashes(db)
            # Context-aware surfacing runs before nags so pulled-forward items send this tick
            now_ts = time.time()
            if now_ts - last_context_eval >= CONTEXT_EVAL_INTERVAL:
                try:
                    from app.context_engine import evaluate_context
                    evaluate_context(db)
                except Exception:
                    log.exception("Context evaluation failed")
                last_context_eval = now_ts
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
