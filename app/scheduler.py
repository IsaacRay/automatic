"""Background scheduler — fires reminders, recurring messages, and action item nags."""

import logging
import time
from datetime import datetime, timedelta, timezone

from sqlalchemy import text

from app.database import engine, Base, SessionLocal
from app.models import Reminder, RecurringSchedule, ActionItem, SmsLog
from app.config import USER_PHONE, USER_TIMEZONE, TICK_SECONDS, GMAIL_SYNC_INTERVAL
from app.twilio_client import send_sms
from app.openai_client import generate_recurring_message
from app.intent_router import _next_cron_fire, _backoff_delta

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)


def _log_outbound(db, body: str, sid: str, related_type: str = None, related_id: int = None):
    db.add(SmsLog(
        direction="outbound",
        phone=USER_PHONE,
        body=body,
        twilio_sid=sid,
        related_type=related_type,
        related_id=related_id,
    ))


def fire_due_reminders(db):
    """Send SMS for all reminders whose fire_at has passed."""
    now = datetime.now(timezone.utc)
    reminders = db.query(Reminder).filter(
        Reminder.status == "pending",
        Reminder.fire_at <= now,
    ).with_for_update(skip_locked=True).all()

    for r in reminders:
        try:
            result = send_sms(r.user_phone, r.message)
            r.status = "sent"
            r.sent_at = now
            _log_outbound(db, r.message, result.get("sid", ""), "reminder", r.id)
            db.commit()
            log.info("Fired reminder #%d: %s", r.id, r.label)
        except Exception:
            log.exception("Failed to fire reminder #%d", r.id)
            db.rollback()


def fire_due_recurring(db):
    """Generate and send messages for due recurring schedules."""
    now = datetime.now(timezone.utc)
    schedules = db.query(RecurringSchedule).filter(
        RecurringSchedule.status == "active",
        RecurringSchedule.next_fire_at <= now,
    ).with_for_update(skip_locked=True).all()

    for s in schedules:
        try:
            message = generate_recurring_message(s.message_prompt)
            result = send_sms(s.user_phone, message)
            s.next_fire_at = _next_cron_fire(s.cron_expression, s.timezone)
            _log_outbound(db, message, result.get("sid", ""), "recurring", s.id)
            db.commit()
            log.info("Fired recurring #%d: %s", s.id, s.label)
        except Exception:
            log.exception("Failed to fire recurring #%d", s.id)
            db.rollback()


def fire_action_item_nags(db):
    """Send re-reminders for pending action items."""
    now = datetime.now(timezone.utc)
    items = db.query(ActionItem).filter(
        ActionItem.status == "pending",
        ActionItem.next_remind_at <= now,
        # Respect snooze
        (ActionItem.snooze_until == None) | (ActionItem.snooze_until <= now),
    ).with_for_update(skip_locked=True).all()

    for item in items:
        try:
            nag_num = item.remind_count + 1
            msg = f"Reminder #{nag_num}: {item.description}"
            if item.source_ref:
                msg += f"\n(from: {item.source_ref})"
            msg += "\nReply DONE to mark complete, SNOOZE to delay."

            result = send_sms(item.user_phone, msg)
            item.remind_count = nag_num
            item.next_remind_at = now + _backoff_delta(nag_num)
            _log_outbound(db, msg, result.get("sid", ""), "action_item", item.id)
            db.commit()
            log.info("Nagged action item #%d (count=%d)", item.id, nag_num)
        except Exception:
            log.exception("Failed to nag action item #%d", item.id)
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

    # Create tables on startup
    Base.metadata.create_all(engine)

    last_gmail_sync = 0.0

    while True:
        db = SessionLocal()
        try:
            fire_due_reminders(db)
            fire_due_recurring(db)
            fire_action_item_nags(db)
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
