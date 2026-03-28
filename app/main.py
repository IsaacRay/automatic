"""FastAPI webhook for incoming Twilio SMS."""

import json
import logging
from datetime import datetime, timezone

from fastapi import FastAPI, Form, Response

from app.database import engine, Base, SessionLocal
from app.models import SmsLog, PendingConfirmation
from app.config import USER_PHONE
from app.openai_client import parse_user_sms
from app.intent_router import handle_intent, execute_reschedule, execute_cancel, execute_acknowledge, execute_acknowledge_all, _handle_create_nag
from app.twilio_client import send_sms

KATHRYN_PHONE = "+19739787648"

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

app = FastAPI(title="ADHD SMS Bot")

EMPTY_TWIML = '<?xml version="1.0" encoding="UTF-8"?><Response></Response>'


@app.on_event("startup")
def on_startup():
    Base.metadata.create_all(engine)
    log.info("Database tables created/verified.")


@app.get("/health")
def health():
    return {"status": "ok"}


@app.post("/sms")
async def incoming_sms(
    From: str = Form(...),
    Body: str = Form(...),
    MessageSid: str = Form(default=""),
):
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
                "interval_minutes": 120,
                "first_nag_at": now_iso,
            })
            # Send the first nag immediately to the user
            send_sms(USER_PHONE, f"Reminder: {label}")
            # Send confirmation to the 973 number
            send_sms(KATHRYN_PHONE, f"Reminder created: \"{label}\" (every 2 hrs)")
            db.add(SmsLog(direction="outbound", phone=USER_PHONE, body=reply, twilio_sid=""))
            db.commit()
        except Exception:
            log.exception("Error processing auto-nag SMS")
            db.rollback()
        finally:
            db.close()
        return Response(content=EMPTY_TWIML, media_type="application/xml")

    # Only allow the configured user phone
    if From != USER_PHONE:
        log.warning("Rejected SMS from unauthorized number: %s", From)
        return Response(content=EMPTY_TWIML, media_type="application/xml")

    log.info("Inbound SMS from %s: %s", From, Body[:100])

    # Relay message to Kathryn if prefixed with "kk"
    stripped = Body.strip()
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
            # Extract payload BEFORE deleting to avoid stale-object issues
            payload = json.loads(pending.payload)
            action_type = pending.action_type
            db.delete(pending)
            db.commit()

            if stripped.startswith("y"):
                # User confirmed — execute the action
                if action_type == "reschedule":
                    reply = execute_reschedule(db, payload)
                elif action_type == "cancel":
                    reply = execute_cancel(db, payload)
                elif action_type == "acknowledge":
                    reply = execute_acknowledge(db, payload)
                elif action_type == "acknowledge_all":
                    reply = execute_acknowledge_all(db, payload)
                else:
                    reply = "Unknown confirmation type."
                log.info("Confirmation accepted: %s", action_type)
            else:
                # User declined — let them know and invite retry
                log.info("Confirmation declined for: %s", action_type)
                label = payload.get("label") or payload.get("description", "that")
                reply = f"OK, didn't {action_type} \"{label}\". Try again with more detail or text LIST to see your items."

            # Send reply
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
