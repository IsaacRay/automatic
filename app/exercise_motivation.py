"""Exercise motivation — daily SMS messages to build an exercise routine."""

import logging
from datetime import datetime

from app.config import USER_TIMEZONE
from app.openai_client import _chat
from app.morning_briefing import fetch_weather

log = logging.getLogger(__name__)


def generate_exercise_morning_message() -> str:
    """Generate a short encouraging SMS to get into the right mindset for exercise."""
    try:
        from zoneinfo import ZoneInfo
        now_local = datetime.now(ZoneInfo(USER_TIMEZONE))
    except Exception:
        now_local = datetime.now()

    system_prompt = (
        "You are sending a short, encouraging SMS to help someone get into the right "
        "mindset for exercise today. Be warm, motivating, and varied day-to-day. "
        "Keep it under 300 characters. No hashtags or emojis. "
        f"Today is {now_local.strftime('%A, %B %d')}."
    )

    return _chat(
        [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": "Send me a motivating message to get me ready for exercise today."},
        ],
        temperature=0.8,
    )


def generate_exercise_evening_message() -> str:
    """Generate a weather-aware SMS recommending an exercise activity."""
    try:
        weather = fetch_weather()
    except Exception:
        log.exception("Failed to fetch weather for exercise evening message")
        weather = "(Weather data unavailable)"

    try:
        from zoneinfo import ZoneInfo
        now_local = datetime.now(ZoneInfo(USER_TIMEZONE))
    except Exception:
        now_local = datetime.now()

    system_prompt = (
        "You are sending a short SMS recommending an exercise activity for this evening. "
        "Based on the weather data provided:\n"
        "- If weather is good (no rain, temp roughly 40-90°F): recommend an outdoor run or outdoor bike ride.\n"
        "- If weather is bad (rain, extreme heat/cold): recommend an indoor bike ride.\n"
        "Be motivating and direct. Keep it under 400 characters. No hashtags or emojis. "
        f"Today is {now_local.strftime('%A, %B %d')}."
    )

    return _chat(
        [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": f"Here's the current weather:\n\n{weather}\n\nWhat should I do for exercise?"},
        ],
        temperature=0.8,
    )
