"""Context-aware surfacing — decide which open to-do items fit the user's
current moment (location/intent + time of day) and pull them forward so the
scheduler's gate/coalescer sends them."""

import json
import logging
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from app.models import NagSchedule, AppState
from app.config import USER_TIMEZONE
from app.openai_client import select_relevant_items

log = logging.getLogger(__name__)


def get_user_context(db) -> str:
    """Return the user's latest location/intent text, or '' if none stored."""
    row = db.query(AppState).filter(AppState.key == "user_context").first()
    if not row or not row.value:
        return ""
    try:
        return json.loads(row.value).get("text", "")
    except (ValueError, TypeError):
        return row.value


def set_user_context(db, text: str):
    """Persist the user's latest location/intent statement to app_state."""
    now = datetime.now(timezone.utc)
    payload = json.dumps({"text": text, "at": now.isoformat()})
    row = db.query(AppState).filter(AppState.key == "user_context").first()
    if row:
        row.value = payload
        row.updated_at = now
    else:
        db.add(AppState(key="user_context", value=payload))


def today_items(db, now: datetime) -> list:
    """Active nags relevant to today: currently in a cycle, or whose next cycle
    is scheduled for today (or earlier)."""
    tz = ZoneInfo(USER_TIMEZONE)
    today = now.astimezone(tz).date()
    nags = db.query(NagSchedule).filter(NagSchedule.status == "active").all()
    items = []
    for nag in nags:
        in_cycle = nag.active_since is not None
        starts_today = nag.next_nag_at.astimezone(tz).date() <= today
        if in_cycle or starts_today:
            items.append(nag)
    return items


def evaluate_context(db) -> list[str]:
    """Surface open items that fit the user's current context by pulling their
    next_nag_at forward to now. Returns the labels surfaced (possibly empty)."""
    now = datetime.now(timezone.utc)
    local_now = now.astimezone(ZoneInfo(USER_TIMEZONE))

    items = today_items(db, now)
    if not items:
        return []

    context_text = get_user_context(db)
    selected = select_relevant_items(
        [{"id": n.id, "label": n.label} for n in items],
        context_text,
        local_now,
    )
    if not selected:
        return []

    by_id = {n.id: n for n in items}
    surfaced = []
    for nag_id in selected:
        nag = by_id.get(nag_id)
        # Only pull forward items not already due; already-due ones fire anyway.
        if nag and nag.next_nag_at > now:
            nag.next_nag_at = now
            surfaced.append(nag.label)
    if surfaced:
        db.commit()
        log.info("Context surfaced %d item(s): %s", len(surfaced), ", ".join(surfaced))
    return surfaced
