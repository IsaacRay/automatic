"""Morning briefing — weather, calendar, and NASDAQ futures via SMS."""

import json
import logging
import urllib.request
from datetime import datetime, date, timedelta, timezone

from app.config import WEATHERAPI_KEY, GOOGLE_CALENDAR_ICS, USER_TIMEZONE
from app.openai_client import _chat

log = logging.getLogger(__name__)


def fetch_weather() -> str:
    """Fetch current weather + today's forecast for Olney, MD (20832)."""
    if not WEATHERAPI_KEY:
        return "(Weather data unavailable — no API key configured)"

    url = (
        "https://api.weatherapi.com/v1/forecast.json"
        f"?key={WEATHERAPI_KEY}&q=20832&days=1&aqi=no&alerts=no"
    )
    req = urllib.request.Request(url)
    with urllib.request.urlopen(req, timeout=15) as resp:
        data = json.loads(resp.read())

    current = data["current"]
    today = data["forecast"]["forecastday"][0]["day"]

    return (
        f"Location: Olney, MD\n"
        f"Current: {current['temp_f']}°F, {current['condition']['text']}\n"
        f"High: {today['maxtemp_f']}°F / Low: {today['mintemp_f']}°F\n"
        f"Chance of rain: {today['daily_chance_of_rain']}%\n"
        f"Humidity: {current['humidity']}%"
    )


def fetch_calendar_events() -> str:
    """Fetch today's Google Calendar events via public ICS feed."""
    if not GOOGLE_CALENDAR_ICS:
        return "(Calendar data unavailable — no ICS URL configured)"

    import icalendar
    import recurring_ical_events
    from zoneinfo import ZoneInfo

    try:
        tz = ZoneInfo(USER_TIMEZONE)
    except Exception:
        tz = timezone.utc

    req = urllib.request.Request(GOOGLE_CALENDAR_ICS)
    with urllib.request.urlopen(req, timeout=15) as resp:
        cal = icalendar.Calendar.from_ical(resp.read())

    today = datetime.now(tz).date()
    start = datetime(today.year, today.month, today.day, tzinfo=tz)
    end = start + timedelta(days=1)

    events = []
    for ev in recurring_ical_events.of(cal).between(start, end):
        summary = str(ev.get("SUMMARY", ""))
        if not summary:
            continue
        dtstart = ev.get("DTSTART").dt
        if isinstance(dtstart, date) and not isinstance(dtstart, datetime):
            events.append(("", summary))
        else:
            dt_local = dtstart.astimezone(tz)
            events.append((dt_local.strftime("%-I:%M %p"), summary))

    if not events:
        return "No events on today's calendar."

    events.sort(key=lambda e: e[0] or "")
    lines = []
    for time_str, summary in events:
        if time_str:
            lines.append(f"- {time_str}: {summary}")
        else:
            lines.append(f"- (all day) {summary}")

    return "Today's calendar:\n" + "\n".join(lines)


def fetch_nasdaq_futures() -> str:
    """Fetch NASDAQ 100 E-mini futures overnight performance."""
    try:
        import yfinance as yf
    except ImportError:
        return "(NASDAQ futures data unavailable — yfinance not installed)"

    ticker = yf.Ticker("NQ=F")
    info = ticker.fast_info

    prev_close = info.previous_close
    current = info.last_price

    if not prev_close or not current:
        return "(NASDAQ futures data unavailable — no price data)"

    change = current - prev_close
    pct_change = (change / prev_close) * 100
    direction = "up" if change >= 0 else "down"

    return (
        f"NASDAQ 100 Futures (NQ=F):\n"
        f"Previous close: {prev_close:,.2f}\n"
        f"Current: {current:,.2f}\n"
        f"Overnight change: {direction} {abs(pct_change):.2f}% ({change:+,.2f} pts)"
    )


def generate_morning_briefing() -> str:
    """Generate the full morning briefing message via GPT-4o."""
    sections = {}

    try:
        sections["weather"] = fetch_weather()
    except Exception:
        log.exception("Failed to fetch weather for morning briefing")
        sections["weather"] = "(Weather data unavailable)"

    try:
        sections["calendar"] = fetch_calendar_events()
    except Exception:
        log.exception("Failed to fetch calendar for morning briefing")
        sections["calendar"] = "(Calendar data unavailable)"

    try:
        sections["nasdaq"] = fetch_nasdaq_futures()
    except Exception:
        log.exception("Failed to fetch NASDAQ futures for morning briefing")
        sections["nasdaq"] = "(NASDAQ futures data unavailable)"

    data_block = (
        f"=== WEATHER ===\n{sections['weather']}\n\n"
        f"=== CALENDAR ===\n{sections['calendar']}\n\n"
        f"=== MARKET ===\n{sections['nasdaq']}"
    )

    try:
        from zoneinfo import ZoneInfo
        now_local = datetime.now(ZoneInfo(USER_TIMEZONE))
    except Exception:
        now_local = datetime.now()

    system_prompt = (
        "You are composing a concise morning briefing SMS. "
        "Summarize the following data into a natural-language message — "
        "friendly, direct, no fluff. Keep it under 400 characters. "
        "Start with a brief greeting referencing the day (e.g. 'Good morning! Here's your Thursday briefing:'). "
        "Cover weather, today's schedule, and market movement in that order. "
        f"Today is {now_local.strftime('%A, %B %d, %Y')}."
    )

    return _chat(
        [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": data_block},
        ],
        temperature=0.5,
    )
