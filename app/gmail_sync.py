"""Gmail sync — fetch emails and create nag schedules for action items."""

import imaplib
import email
import logging
from datetime import datetime, timedelta, timezone
from email.header import decode_header

from app.database import SessionLocal
from app.models import NagSchedule, ProcessedEmail
from app.config import (
    GMAIL_ADDRESS, GMAIL_APP_PASSWORD, GMAIL_SEARCH_FROM,
    GMAIL_SEARCH_DAYS, USER_PHONE, USER_TIMEZONE,
)
from app.openai_client import extract_action_items_structured

log = logging.getLogger(__name__)

IMAP_SERVER = "imap.gmail.com"
IMAP_PORT = 993


def _decode_mime_header(value):
    if value is None:
        return "(unknown)"
    parts = decode_header(value)
    decoded = []
    for part, charset in parts:
        if isinstance(part, bytes):
            decoded.append(part.decode(charset or "utf-8", errors="replace"))
        else:
            decoded.append(part)
    return "".join(decoded)


def _get_body_text(msg):
    if msg.is_multipart():
        for part in msg.walk():
            if part.get_content_type() == "text/plain" and part.get("Content-Disposition") is None:
                charset = part.get_content_charset() or "utf-8"
                return part.get_payload(decode=True).decode(charset, errors="replace")
    else:
        charset = msg.get_content_charset() or "utf-8"
        return msg.get_payload(decode=True).decode(charset, errors="replace")
    return ""


def fetch_emails() -> list[dict]:
    """Fetch recent emails from the configured sender, skipping already-processed ones."""
    if not GMAIL_APP_PASSWORD:
        log.warning("Gmail credentials not configured, skipping sync.")
        return []

    log.info("Connecting to Gmail...")
    mail = imaplib.IMAP4_SSL(IMAP_SERVER, IMAP_PORT)
    mail.login(GMAIL_ADDRESS, GMAIL_APP_PASSWORD)
    mail.select("INBOX")

    since_date = (datetime.now() - timedelta(days=GMAIL_SEARCH_DAYS)).strftime("%d-%b-%Y")
    status, data = mail.search(None, f'(FROM "{GMAIL_SEARCH_FROM}" SINCE {since_date})')

    if status != "OK" or not data[0]:
        log.info("No emails found from %s in the last %d days.", GMAIL_SEARCH_FROM, GMAIL_SEARCH_DAYS)
        mail.logout()
        return []

    email_ids = data[0].split()
    log.info("Found %d emails to check.", len(email_ids))

    # Load already-processed message IDs
    db = SessionLocal()
    try:
        processed = set(
            row.message_id for row in db.query(ProcessedEmail.message_id).all()
        )
    finally:
        db.close()

    emails = []
    for eid in email_ids:
        status, msg_data = mail.fetch(eid, "(BODY.PEEK[])")
        if status != "OK":
            continue
        msg = email.message_from_bytes(msg_data[0][1])

        # Extract Message-ID header for dedup
        message_id = msg.get("Message-ID", "").strip()
        if not message_id:
            # Fallback: construct a pseudo-ID from subject+date
            message_id = f"{msg.get('Subject', '')}|{msg.get('Date', '')}"

        if message_id in processed:
            continue

        subject = _decode_mime_header(msg["Subject"])
        date = msg["Date"] or "(unknown)"
        body = _get_body_text(msg)
        if body:
            emails.append({
                "subject": subject,
                "date": date,
                "body": body,
                "message_id": message_id,
            })

    mail.logout()
    log.info("Found %d new (unprocessed) emails.", len(emails))
    return emails


def sync_gmail_action_items():
    """Fetch emails, extract action items, and store as nag schedules."""
    emails = fetch_emails()
    if not emails:
        return

    log.info("Extracting action items from %d emails...", len(emails))
    items = extract_action_items_structured(emails)

    if not items:
        log.info("No action items found.")
        # Still mark emails as processed so we don't re-analyze them
        _mark_emails_processed(emails)
        return

    db = SessionLocal()
    try:
        now = datetime.now(timezone.utc)
        created = 0
        for item in items:
            desc = item.get("description", "").strip()
            source_ref = item.get("source_ref", "").strip()
            if not desc:
                continue

            # Dedup by source_ref (in case same email produces same action item text)
            existing = db.query(NagSchedule).filter(
                NagSchedule.user_phone == USER_PHONE,
                NagSchedule.source == "gmail",
                NagSchedule.source_ref == source_ref,
            ).first()
            if existing:
                continue

            db.add(NagSchedule(
                user_phone=USER_PHONE,
                label=desc,
                message=desc,
                cron_expression="0 9 * * *",
                interval_minutes=120,
                max_duration_minutes=None,
                timezone=USER_TIMEZONE,
                next_nag_at=now,
                repeating=False,
                source="gmail",
                source_ref=source_ref,
                status="active",
            ))
            created += 1

        db.commit()
        log.info("Created %d new nag schedules from Gmail.", created)
    except Exception:
        log.exception("Error storing action items as nags")
        db.rollback()
    finally:
        db.close()

    # Mark all fetched emails as processed
    _mark_emails_processed(emails)


def _mark_emails_processed(emails: list[dict]):
    """Record email Message-IDs so they won't be re-processed."""
    db = SessionLocal()
    try:
        for e in emails:
            msg_id = e.get("message_id", "")
            if not msg_id:
                continue
            exists = db.query(ProcessedEmail).filter(
                ProcessedEmail.message_id == msg_id
            ).first()
            if not exists:
                db.add(ProcessedEmail(
                    message_id=msg_id,
                    subject=e.get("subject"),
                    date=e.get("date"),
                ))
        db.commit()
    except Exception:
        log.exception("Error marking emails as processed")
        db.rollback()
    finally:
        db.close()
