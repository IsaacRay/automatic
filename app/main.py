"""FastAPI webhook for incoming Twilio SMS."""

import logging
from fastapi import FastAPI, Form, Response

from app.database import engine, Base, SessionLocal
from app.models import SmsLog
from app.config import USER_PHONE
from app.openai_client import parse_user_sms
from app.intent_router import handle_intent
from app.twilio_client import send_sms

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
    # Only allow the configured user phone
    if From != USER_PHONE:
        log.warning("Rejected SMS from unauthorized number: %s", From)
        return Response(content=EMPTY_TWIML, media_type="application/xml")

    log.info("Inbound SMS from %s: %s", From, Body[:100])

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

        # Parse intent via OpenAI
        parsed = parse_user_sms(Body)
        log.info("Parsed intent: %s", parsed.get("intent"))

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
