"""Centralized Twilio SMS sending."""

import base64
import json
import urllib.request
import urllib.parse

from app.config import TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN, TWILIO_FROM_NUMBER


def send_sms(to: str, body: str) -> dict:
    """Send an SMS via the Twilio REST API. Returns the API response dict."""
    url = f"https://api.twilio.com/2010-04-01/Accounts/{TWILIO_ACCOUNT_SID}/Messages.json"
    data = urllib.parse.urlencode({
        "To": to,
        "From": TWILIO_FROM_NUMBER,
        "Body": body,
    }).encode()
    credentials = base64.b64encode(
        f"{TWILIO_ACCOUNT_SID}:{TWILIO_AUTH_TOKEN}".encode()
    ).decode()
    req = urllib.request.Request(url, data=data, headers={
        "Authorization": f"Basic {credentials}",
    })
    with urllib.request.urlopen(req) as resp:
        return json.loads(resp.read())
