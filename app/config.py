"""Configuration — env vars with file-based fallback for credentials."""

import os


def _env_or_file(env_key: str, file_path: str, *, strip: bool = True) -> str:
    """Return env var value if set, otherwise read from file."""
    val = os.environ.get(env_key)
    if val:
        return val
    try:
        with open(file_path) as f:
            contents = f.read()
            return contents.strip() if strip else contents
    except FileNotFoundError:
        return ""


def _twilio_field(index: int) -> str:
    """Read a specific line from the Twilio credentials file."""
    val_map = {0: "TWILIO_ACCOUNT_SID", 1: "TWILIO_AUTH_TOKEN", 2: "TWILIO_FROM_NUMBER"}
    env_val = os.environ.get(val_map[index])
    if env_val:
        return env_val
    try:
        with open("/home/iray/twilio_cred.txt") as f:
            lines = f.read().strip().splitlines()
            return lines[index].strip()
    except (FileNotFoundError, IndexError):
        return ""


DATABASE_URL = os.environ.get(
    "DATABASE_URL",
    "postgresql+psycopg2://adhdbot:change-me@localhost:5432/adhdbot",
)
OPENAI_API_KEY = _env_or_file("OPENAI_API_KEY", "/home/iray/openai_key.txt")
TWILIO_ACCOUNT_SID = _twilio_field(0)
TWILIO_AUTH_TOKEN = _twilio_field(1)
TWILIO_FROM_NUMBER = _twilio_field(2)
USER_PHONE = os.environ.get("USER_PHONE", "+15184690834")
USER_TIMEZONE = os.environ.get("USER_TIMEZONE", "America/New_York")
TICK_SECONDS = int(os.environ.get("TICK_SECONDS", "60"))
GMAIL_SYNC_INTERVAL = int(os.environ.get("GMAIL_SYNC_INTERVAL", "1800"))  # 30 min

# Gmail credentials
GMAIL_ADDRESS = os.environ.get("GMAIL_ADDRESS", "isaacmray1984@gmail.com")
GMAIL_APP_PASSWORD = _env_or_file("GMAIL_APP_PASSWORD", "/home/iray/app_cred.txt")
GMAIL_SEARCH_FROM = os.environ.get("GMAIL_SEARCH_FROM", "kathrynrose6@gmail.com")
GMAIL_SEARCH_DAYS = int(os.environ.get("GMAIL_SEARCH_DAYS", "30"))

# Morning briefing
WEATHERAPI_KEY = _env_or_file("WEATHERAPI_KEY", "/home/iray/weatherapi_key.txt")
BRIEFING_TIME = os.environ.get("BRIEFING_TIME", "07:30")

# Exercise motivation
EXERCISE_MORNING_TIME = os.environ.get("EXERCISE_MORNING_TIME", "10:00")
EXERCISE_EVENING_TIME = os.environ.get("EXERCISE_EVENING_TIME", "17:00")
GOOGLE_CALENDAR_ICS = os.environ.get(
    "GOOGLE_CALENDAR_ICS",
    "https://calendar.google.com/calendar/ical/isaacmray1984%40gmail.com/private-6c66bacfc8241c9fa97fe2985e7b2ba7/basic.ics",
)

# Basement light webhooks (IFTTT)
BASEMENT_LIGHT_ON = os.environ.get("BASEMENT_LIGHT_ON", "")
BASEMENT_LIGHT_OFF = os.environ.get("BASEMENT_LIGHT_OFF", "")
