"""
Gmail Reader - Fetch emails from a specific sender and extract action items.

Reads emails from kathrynrose6@gmail.com (last 30 days) and uses the
OpenAI API to extract action items from the email bodies.
"""

import imaplib
import email
import json
import base64
import urllib.request
import urllib.parse
from datetime import datetime, timedelta
from email.header import decode_header


IMAP_SERVER = "imap.gmail.com"
IMAP_PORT = 993


def decode_mime_header(value):
    """Decode a MIME-encoded header into a readable string."""
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


def get_body_text(msg):
    """Extract plain text body from an email message."""
    if msg.is_multipart():
        for part in msg.walk():
            if part.get_content_type() == "text/plain" and part.get("Content-Disposition") is None:
                charset = part.get_content_charset() or "utf-8"
                return part.get_payload(decode=True).decode(charset, errors="replace")
    else:
        charset = msg.get_content_charset() or "utf-8"
        return msg.get_payload(decode=True).decode(charset, errors="replace")
    return ""


def extract_action_items(api_key, emails):
    """Send email bodies to OpenAI and extract action items."""
    email_text = ""
    for i, e in enumerate(emails, 1):
        email_text += f"--- Email {i} (Date: {e['date']}, Subject: {e['subject']}) ---\n"
        email_text += e["body"] + "\n\n"

    payload = json.dumps({
        "model": "gpt-4o",
        "messages": [
            {
                "role": "system",
                "content": "You are analyzing emails sent from Kathryn to Isaac. For each email, determine if Kathryn is asking or expecting Isaac to do something — a task, a reply, a decision, a favor, etc. List each action item clearly and concisely, noting which email it came from (by date and subject). If an email is purely informational with nothing for Isaac to act on, skip it. If none of the emails require action, say so."
            },
            {
                "role": "user",
                "content": f"Extract all action items from the following emails:\n\n{email_text}"
            }
        ]
    }).encode()

    req = urllib.request.Request(
        "https://api.openai.com/v1/chat/completions",
        data=payload,
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {api_key}",
        },
    )
    with urllib.request.urlopen(req) as resp:
        result = json.loads(resp.read())

    return result["choices"][0]["message"]["content"]


def send_sms(account_sid, auth_token, from_number, to_number, message):
    """Send an SMS via the Twilio API."""
    url = f"https://api.twilio.com/2010-04-01/Accounts/{account_sid}/Messages.json"
    data = urllib.parse.urlencode({
        "To": to_number,
        "From": from_number,
        "Body": message,
    }).encode()
    credentials = base64.b64encode(f"{account_sid}:{auth_token}".encode()).decode()
    req = urllib.request.Request(url, data=data, headers={
        "Authorization": f"Basic {credentials}",
    })
    with urllib.request.urlopen(req) as resp:
        return json.loads(resp.read())


def main():
    address = "isaacmray1984@gmail.com"
    with open("/home/iray/app_cred.txt") as f:
        app_password = f.read().strip()
    with open("/home/iray/openai_key.txt") as f:
        openai_key = f.read().strip()
    with open("/home/iray/twilio_cred.txt") as f:
        twilio_lines = f.read().strip().splitlines()
        twilio_sid = twilio_lines[0].strip()
        twilio_token = twilio_lines[1].strip()
        twilio_from = twilio_lines[2].strip()

    print("Connecting to Gmail...")
    mail = imaplib.IMAP4_SSL(IMAP_SERVER, IMAP_PORT)
    mail.login(address, app_password)
    mail.select("INBOX")

    since_date = (datetime.now() - timedelta(days=30)).strftime("%d-%b-%Y")
    status, data = mail.search(None, f'(FROM "kathrynrose6@gmail.com" SINCE {since_date})')
    if status != "OK" or not data[0]:
        print("No emails found from kathrynrose6@gmail.com in the last 30 days.")
        mail.logout()
        return

    email_ids = data[0].split()
    print(f"Found {len(email_ids)} emails. Fetching...")

    emails = []
    for eid in email_ids:
        status, msg_data = mail.fetch(eid, "(BODY.PEEK[])")
        if status != "OK":
            continue

        msg = email.message_from_bytes(msg_data[0][1])
        subject = decode_mime_header(msg["Subject"])
        date = msg["Date"] or "(unknown)"
        body = get_body_text(msg)

        if body:
            emails.append({"subject": subject, "date": date, "body": body})

    mail.logout()

    if not emails:
        print("No email content to analyze.")
        return

    print("Extracting action items...\n")
    action_items = extract_action_items(openai_key, emails)
    print(action_items)

    print("\nSending SMS...")
    send_sms(twilio_sid, twilio_token, twilio_from, "+15184690834", action_items)
    print("SMS sent.")


if __name__ == "__main__":
    main()
