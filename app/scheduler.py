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
    DIGEST_MIN_GAP, DIGEST_MAX_GAP, OVERDUE_PING_GAP,
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


def _format_clock(dt) -> str:
    """Local clock time like '5:00 PM' for a UTC datetime."""
    from zoneinfo import ZoneInfo
    return dt.astimezone(ZoneInfo(USER_TIMEZONE)).strftime("%-I:%M %p")


def _effective_deadline(nag, now):
    """The item's expire time: explicit deadline_at, else this cycle's computed
    deadline for a repeating item. None if not computable."""
    from app.context_engine import cycle_deadline
    return cycle_deadline(nag, now)


def _enqueue_burst(db, messages):
    """Append messages to the pending-burst queue, drained one per minute
    (gate-exempt) by drain_burst."""
    import json
    raw = _get_state(db, "pending_burst")
    queue = json.loads(raw) if raw else []
    queue.extend(messages)
    _set_state(db, "pending_burst", json.dumps(queue))


def drain_burst(db):
    """Send at most one queued burst message per ~minute. Bursts bypass the
    digest cadence, but still hold during quiet hours."""
    import json
    now = datetime.now(timezone.utc)
    raw = _get_state(db, "pending_burst")
    queue = json.loads(raw) if raw else []
    if not queue or _is_quiet_hours(USER_TIMEZONE):
        return
    last = _get_state(db, "burst_last_sent")
    if last:
        try:
            if (now - datetime.fromisoformat(last)).total_seconds() < 50:
                return
        except ValueError:
            pass
    msg = queue.pop(0)
    try:
        result = send_sms(USER_PHONE, msg)
        _set_state(db, "pending_burst", json.dumps(queue))
        _set_state(db, "burst_last_sent", now.isoformat())
        _log_outbound(db, msg, result.get("sid", ""))
        db.commit()
        log.info("Drained burst message (%d left)", len(queue))
    except Exception:
        log.exception("Failed to send burst message")
        db.rollback()


def fire_cycle_starts(db):
    """Activate dormant nags whose scheduled start has arrived: set active_since
    and this cycle's deadline, then point next_nag_at at the next cycle so the
    item doesn't re-trigger. Sends nothing — the digest surfaces active items."""
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
            # Advance to the next cycle so this dormant trigger doesn't refire today.
            if nag.repeating and nag.cron_expression:
                nag.next_nag_at = _next_cron_fire(nag.cron_expression, nag.timezone)
            else:
                nag.next_nag_at = now + timedelta(days=3650)
            db.commit()
            log.info("Nag #%d cycle started: %s", nag.id, nag.label)
        except Exception:
            log.exception("Failed to start cycle for nag #%d", nag.id)
            db.rollback()


def fire_due_bursts(db):
    """Arm the T-3/T-2/T-1 due burst: when an open item's expire time is within
    the next 3 minutes, enqueue three once-a-minute alerts (coalescing items that
    arm in the same tick). Each item arms once per deadline."""
    from app.context_engine import today_items, is_done_today
    now = datetime.now(timezone.utc)
    if _is_quiet_hours(USER_TIMEZONE):
        return

    arming = []
    for nag in today_items(db, now):
        if nag.burst_armed or is_done_today(nag, now):
            continue
        dl = _effective_deadline(nag, now)
        if dl is None:
            continue
        secs = (dl - now).total_seconds()
        if 0 < secs <= 180:
            arming.append((nag, dl))

    if not arming:
        return

    try:
        for nag, _dl in arming:
            nag.burst_armed = True
        labels = ", ".join(f'"{n.label}" (by {_format_clock(dl)})' for n, dl in arming)
        msgs = [
            f"DUE SOON ({i}/3): {labels}. Finish up now — reply DONE <item> when done."
            for i in range(1, 4)
        ]
        _enqueue_burst(db, msgs)
        db.commit()
        log.info("Armed due burst for %d item(s): %s",
                 len(arming), ", ".join(n.label for n, _dl in arming))
    except Exception:
        log.exception("Failed to arm due burst")
        db.rollback()


def _schedule_next_digest(db, now):
    """Set next_digest_at to a fresh random gap from now."""
    gap = random.randint(DIGEST_MIN_GAP, DIGEST_MAX_GAP)
    _set_state(db, "next_digest_at", (now + timedelta(minutes=gap)).isoformat())


def _build_digest_message(items, now) -> str:
    """One digest SMS: numbered open items + expire times, with a GPT plan line."""
    pairs = [(n.label, _effective_deadline(n, now)) for n in items]
    lines = ["Your list:"]
    for i, (label, dl) in enumerate(pairs, 1):
        when = f" — by {_format_clock(dl)}" if dl else ""
        lines.append(f"{i}) {label}{when}")
    body = "\n".join(lines)
    try:
        from app.openai_client import generate_nag_plan
        plan = generate_nag_plan([label for label, _dl in pairs])
    except Exception:
        log.exception("GPT digest plan failed, omitting plan line")
        plan = ""
    if plan:
        body += f"\n\n{plan}"
    body += "\n\nReply DONE <item> when finished."
    return body


def fire_digests(db):
    """Random-cadence digest: every DIGEST_MIN_GAP..DIGEST_MAX_GAP minutes, send
    one SMS listing every open today item and its expire time."""
    from app.context_engine import today_items, is_done_today
    now = datetime.now(timezone.utc)

    nd = _get_state(db, "next_digest_at")
    if nd is None:
        _schedule_next_digest(db, now)
        db.commit()
        return
    try:
        if now < datetime.fromisoformat(nd):
            return
    except ValueError:
        _schedule_next_digest(db, now)
        db.commit()
        return

    # Hold (without rescheduling) during quiet hours so the first digest lands
    # right when quiet hours end.
    if _is_quiet_hours(USER_TIMEZONE):
        return

    items = [n for n in today_items(db, now) if not is_done_today(n, now)]
    _schedule_next_digest(db, now)
    if not items:
        db.commit()
        return

    try:
        msg = _build_digest_message(items, now)
        result = send_sms(USER_PHONE, msg)
        _log_outbound(db, msg, result.get("sid", ""))
        db.commit()
        log.info("Sent digest with %d item(s)", len(items))
    except Exception:
        log.exception("Failed to send digest")
        db.rollback()


def _overdue_items(db, now):
    """Active, not-yet-done items whose expire time has already passed."""
    from app.context_engine import today_items, is_done_today
    out = []
    for nag in today_items(db, now):
        if nag.active_since is None or is_done_today(nag, now):
            continue
        dl = _effective_deadline(nag, now)
        if dl is not None and dl <= now:
            out.append(nag)
    return out


def _build_overdue_message(items, now) -> str:
    """One overdue ping listing every past-due open item and when it was due."""
    parts = ", ".join(
        f'"{n.label}" (was due {_format_clock(_effective_deadline(n, now))})'
        for n in items
    )
    return (f"OVERDUE: {parts}. Reply DONE <item> when finished, "
            f"or snooze to push it back.")


def fire_overdue_pings(db):
    """After an item's due burst, if it's still open keep pinging every
    OVERDUE_PING_GAP minutes until it's checked off or snoozed. Coalesces all
    past-due items into one ping. Holds (without rescheduling) during quiet
    hours, like the digest."""
    now = datetime.now(timezone.utc)
    items = _overdue_items(db, now)

    # Nothing overdue — clear the timer so the next overdue item starts a fresh
    # OVERDUE_PING_GAP countdown rather than firing immediately.
    if not items:
        if _get_state(db, "next_overdue_ping_at") is not None:
            db.query(AppState).filter(AppState.key == "next_overdue_ping_at").delete()
            db.commit()
        return

    nop = _get_state(db, "next_overdue_ping_at")
    if nop is None:
        # First time we notice overdue items: wait one gap before the first ping
        # (gives the T-1 burst message room to land).
        _set_state(db, "next_overdue_ping_at",
                   (now + timedelta(minutes=OVERDUE_PING_GAP)).isoformat())
        db.commit()
        return
    try:
        if now < datetime.fromisoformat(nop):
            return
    except ValueError:
        _set_state(db, "next_overdue_ping_at",
                   (now + timedelta(minutes=OVERDUE_PING_GAP)).isoformat())
        db.commit()
        return

    # Hold during quiet hours without advancing, so pinging resumes at 6 AM.
    if _is_quiet_hours(USER_TIMEZONE):
        return

    try:
        msg = _build_overdue_message(items, now)
        result = send_sms(USER_PHONE, msg)
        _log_outbound(db, msg, result.get("sid", ""))
        _set_state(db, "next_overdue_ping_at",
                   (now + timedelta(minutes=OVERDUE_PING_GAP)).isoformat())
        db.commit()
        log.info("Sent overdue ping for %d item(s): %s",
                 len(items), ", ".join(n.label for n in items))
    except Exception:
        log.exception("Failed to send overdue ping")
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
    rollover: surface items missed the previous day as a once-a-minute burst and
    reset their cycles."""
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
            _enqueue_burst(db, [
                f"MISSED ({i}/3): you didn't finish {joined} yesterday. "
                f"Carried to today — reply DONE <item> when handled."
                for i in range(1, 4)
            ])
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
            fire_due_bursts(db)     # arm T-3 due bursts into the queue
            fire_overdue_pings(db)  # every 5 min past expire until done/snoozed
            fire_digests(db)        # random-cadence list digest
            drain_burst(db)         # send <=1 queued burst message/minute
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
