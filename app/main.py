"""FastAPI webhook for incoming Twilio SMS."""

import json
import logging
import os
import uuid
from datetime import datetime, timezone

import requests as http_requests
from fastapi import FastAPI, Form, Request, Response

from app.database import SessionLocal
from app.models import SmsLog, PendingConfirmation, DailyChecklistItem
from app.config import USER_PHONE, TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN
from app.openai_client import parse_user_sms
from app.intent_router import handle_intent, undo_reschedule, undo_cancel, undo_acknowledge, undo_acknowledge_all, undo_snooze, _handle_create_nag
from app.twilio_client import send_sms

PHOTOS_DIR = "/app/photos"

KATHRYN_PHONE = "+19739787648"

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

app = FastAPI(title="ADHD SMS Bot")

EMPTY_TWIML = '<?xml version="1.0" encoding="UTF-8"?><Response></Response>'


@app.on_event("startup")
def on_startup():
    from app.migrations import run_migrations
    run_migrations()


@app.get("/health")
def health():
    return {"status": "ok"}


def _save_mms_images(form_data: dict) -> int:
    """Download MMS media attachments and save to PHOTOS_DIR. Returns count saved."""
    num_media = int(form_data.get("NumMedia", "0"))
    if num_media == 0:
        return 0

    os.makedirs(PHOTOS_DIR, exist_ok=True)
    saved = 0

    for i in range(num_media):
        media_url = form_data.get(f"MediaUrl{i}")
        content_type = form_data.get(f"MediaContentType{i}", "")

        if not media_url or not content_type.startswith("image/"):
            continue

        ext_map = {
            "image/jpeg": ".jpg", "image/png": ".png",
            "image/gif": ".gif", "image/webp": ".webp",
        }
        ext = ext_map.get(content_type, ".jpg")

        try:
            resp = http_requests.get(
                media_url,
                auth=(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN),
                timeout=30,
            )
            resp.raise_for_status()

            timestamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
            filename = f"{timestamp}_{uuid.uuid4().hex[:8]}{ext}"
            filepath = os.path.join(PHOTOS_DIR, filename)

            with open(filepath, "wb") as f:
                f.write(resp.content)

            saved += 1
            log.info("Saved MMS image: %s (%s, %d bytes)", filename, content_type, len(resp.content))
        except Exception:
            log.exception("Failed to download MMS media: %s", media_url)

    return saved


@app.post("/sms")
async def incoming_sms(request: Request):
    form_data = await request.form()
    From = form_data.get("From", "")
    Body = form_data.get("Body", "")
    MessageSid = form_data.get("MessageSid", "")
    # Auto-create nag from special number
    if From == KATHRYN_PHONE:
        log.info("Auto-nag SMS from %s: %s", From, Body[:100])
        db = SessionLocal()
        try:
            db.add(SmsLog(direction="inbound", phone=From, body=Body, twilio_sid=MessageSid))
            db.commit()
            label = Body.strip()
            now_iso = datetime.now(timezone.utc).isoformat()
            reply = _handle_create_nag(db, {
                "label": label,
                "message": f"Reminder: {label}",
                "first_nag_at": now_iso,
            })
            # Send the first nag immediately to the user
            send_sms(USER_PHONE, f"Reminder: {label}")
            # Send confirmation to the 973 number
            send_sms(KATHRYN_PHONE, f"Reminder created: \"{label}\" (deadline 11pm)")
            db.add(SmsLog(direction="outbound", phone=USER_PHONE, body=reply, twilio_sid=""))
            db.commit()
        except Exception:
            log.exception("Error processing auto-nag SMS")
            db.rollback()
        finally:
            db.close()
        return Response(content=EMPTY_TWIML, media_type="application/xml")

    # Anyone can send photos for the slideshow — save MMS images regardless of sender
    saved_count = _save_mms_images(dict(form_data))
    if saved_count > 0:
        photo_reply = f"{'Photo' if saved_count == 1 else f'{saved_count} photos'} saved!"
        send_sms(From, photo_reply)
        db = SessionLocal()
        try:
            db.add(SmsLog(direction="inbound", phone=From, body=Body, twilio_sid=MessageSid))
            db.add(SmsLog(direction="outbound", phone=From, body=photo_reply, twilio_sid=""))
            db.commit()
        except Exception:
            log.exception("Error logging slideshow MMS")
            db.rollback()
        finally:
            db.close()
        # Ignore any text body — just save the photos
        return Response(content=EMPTY_TWIML, media_type="application/xml")

    # Only allow the configured user phone
    if From != USER_PHONE:
        log.warning("Rejected SMS from unauthorized number: %s", From)
        return Response(content=EMPTY_TWIML, media_type="application/xml")

    log.info("Inbound SMS from %s: %s", From, Body[:100])

    stripped = Body.strip()

    # Add to daily checklist if prefixed with "##"
    if stripped.startswith("##"):
        item_label = stripped[2:].strip()
        if item_label:
            db = SessionLocal()
            try:
                db.add(SmsLog(direction="inbound", phone=From, body=Body, twilio_sid=MessageSid))
                db.add(DailyChecklistItem(label=item_label))
                db.commit()
                reply = f'Added to checklist: "{item_label}"'
                result = send_sms(USER_PHONE, reply)
                db.add(SmsLog(direction="outbound", phone=USER_PHONE, body=reply, twilio_sid=result.get("sid", "")))
                db.commit()
            except Exception:
                log.exception("Error adding checklist item")
                db.rollback()
            finally:
                db.close()
            return Response(content=EMPTY_TWIML, media_type="application/xml")

    # Relay message to Kathryn if prefixed with "kk"
    if stripped[:2].lower() == "kk":
        relay_body = stripped[2:].strip()
        if relay_body:
            db = SessionLocal()
            try:
                db.add(SmsLog(direction="inbound", phone=From, body=Body, twilio_sid=MessageSid))
                result = send_sms(KATHRYN_PHONE, relay_body)
                db.add(SmsLog(direction="outbound", phone=KATHRYN_PHONE, body=relay_body, twilio_sid=result.get("sid", "")))
                db.commit()
                log.info("Relayed message to %s: %s", KATHRYN_PHONE, relay_body[:80])
            except Exception:
                log.exception("Error relaying kk message")
                db.rollback()
            finally:
                db.close()
            return Response(content=EMPTY_TWIML, media_type="application/xml")

    db = SessionLocal()
    try:
        # Log inbound
        db.add(SmsLog(
            direction="inbound",
            phone=From,
            body=Body,
            twilio_sid=MessageSid,
        ))
        db.commit()

        # Check for pending confirmation before parsing intent
        now = datetime.now(timezone.utc)
        pending = db.query(PendingConfirmation).filter(
            PendingConfirmation.user_phone == From,
            PendingConfirmation.expires_at > now,
        ).order_by(PendingConfirmation.created_at.desc()).first()

        if pending:
            stripped = Body.strip().lower()

            if stripped.startswith("undo"):
                # Extract payload BEFORE deleting to avoid stale-object issues
                payload = json.loads(pending.payload)
                action_type = pending.action_type
                db.delete(pending)
                db.commit()

                # User wants to undo — dispatch to the appropriate undo function
                undo_handlers = {
                    "undo_reschedule": undo_reschedule,
                    "undo_cancel": undo_cancel,
                    "undo_acknowledge": undo_acknowledge,
                    "undo_acknowledge_all": undo_acknowledge_all,
                    "undo_snooze": undo_snooze,
                }
                handler = undo_handlers.get(action_type)
                if handler:
                    reply = handler(db, payload)
                else:
                    reply = "Nothing to undo."
                log.info("Undo accepted: %s", action_type)

                result = send_sms(USER_PHONE, reply)
                sid = result.get("sid", "")
                db.add(SmsLog(
                    direction="outbound",
                    phone=USER_PHONE,
                    body=reply,
                    twilio_sid=sid,
                ))
                db.commit()

                return Response(content=EMPTY_TWIML, media_type="application/xml")

            # Not an undo — clear the pending confirmation and fall through
            # to normal intent parsing so the new message is handled normally
            db.delete(pending)
            db.commit()

        # Also clean up any expired confirmations
        db.query(PendingConfirmation).filter(
            PendingConfirmation.expires_at <= now,
        ).delete()
        db.commit()

        # Parse intent via OpenAI
        parsed = parse_user_sms(Body)
        log.info("Parsed intent: %s", parsed.get("intent"))

        # Inject raw message body so handlers always have the original text
        parsed.setdefault("data", {})["_raw_message"] = Body

        # Handle intent
        reply = handle_intent(db, parsed)

        # Send reply
        result = send_sms(USER_PHONE, reply)
        sid = result.get("sid", "")

        # Log outbound
        db.add(SmsLog(
            direction="outbound",
            phone=USER_PHONE,
            body=reply,
            twilio_sid=sid,
        ))
        db.commit()

    except Exception:
        log.exception("Error processing SMS")
        db.rollback()
        try:
            error_reply = "Something went wrong processing your message. Try again?"
            send_sms(USER_PHONE, error_reply)
        except Exception:
            log.exception("Failed to send error reply")
    finally:
        db.close()

    return Response(content=EMPTY_TWIML, media_type="application/xml")
