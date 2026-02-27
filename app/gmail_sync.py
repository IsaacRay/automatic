"""Gmail sync — fetch emails and store structured action items in DB."""

import imaplib
import email
import logging
from datetime import datetime, timedelta, timezone
from email.header import decode_header

from app.database import SessionLocal
from app.models import ActionItem
from app.config import (
    GMAIL_ADDRESS, GMAIL_APP_PASSWORD, GMAIL_SEARCH_FROM,
    GMAIL_SEARCH_DAYS, USER_PHONE,
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
    """Fetch recent emails from the configured sender."""
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
    log.info("Found %d emails to process.", len(email_ids))

    emails = []
    for eid in email_ids:
        status, msg_data = mail.fetch(eid, "(BODY.PEEK[])")
        if status != "OK":
            continue
        msg = email.message_from_bytes(msg_data[0][1])
        subject = _decode_mime_header(msg["Subject"])
        date = msg["Date"] or "(unknown)"
        body = _get_body_text(msg)
        if body:
            emails.append({"subject": subject, "date": date, "body": body})

    mail.logout()
    return emails


def sync_gmail_action_items():
    """Fetch emails, extract action items, and store new ones in DB."""
    emails = fetch_emails()
    if not emails:
        return

    log.info("Extracting action items from %d emails...", len(emails))
    items = extract_action_items_structured(emails)

    if not items:
        log.info("No action items found.")
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

            # Dedup by source_ref alone
            existing = db.query(ActionItem).filter(
                ActionItem.user_phone == USER_PHONE,
                ActionItem.source == "gmail",
                ActionItem.source_ref == source_ref,
            ).first()
            if existing:
                continue

            db.add(ActionItem(
                user_phone=USER_PHONE,
                source="gmail",
                source_ref=source_ref,
                description=desc,
                status="pending",
                remind_count=0,
                next_remind_at=now,
            ))
            created += 1

        db.commit()
        log.info("Created %d new action items from Gmail.", created)
    except Exception:
        log.exception("Error storing action items")
        db.rollback()
    finally:
        db.close()
