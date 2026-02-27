"""Gmail Reader — thin wrapper around app.gmail_sync.

Can still be run standalone: python gmail_reader.py
"""

from app.gmail_sync import fetch_emails
from app.openai_client import extract_action_items_structured
from app.twilio_client import send_sms
from app.config import USER_PHONE


def main():
    emails = fetch_emails()
    if not emails:
        print("No emails to process.")
        return

    print(f"Processing {len(emails)} emails...")
    items = extract_action_items_structured(emails)

    if not items:
        print("No action items found.")
        return

    summary = "\n".join(f"- {item['description']}" for item in items)
    print(summary)

    print("\nSending SMS...")
    send_sms(USER_PHONE, f"Action items from email:\n{summary}")
    print("SMS sent.")


if __name__ == "__main__":
    main()
