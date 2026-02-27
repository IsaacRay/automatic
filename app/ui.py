"""Bare-bones web UI for viewing and deleting reminders. Runs on port 8081."""

from datetime import datetime, timezone

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from app.database import engine, Base, SessionLocal
from app.models import Reminder, RecurringSchedule, ActionItem
from app.config import USER_TIMEZONE

app = FastAPI(title="ADHD Bot UI")


def _fmt(dt):
    """Format a UTC datetime to local time string."""
    if dt is None:
        return "-"
    from zoneinfo import ZoneInfo
    return dt.astimezone(ZoneInfo(USER_TIMEZONE)).strftime("%a %b %d %I:%M %p")


def _render_page(body: str) -> HTMLResponse:
    html = f"""<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>ADHD Bot</title>
<style>
  body {{ font-family: monospace; max-width: 900px; margin: 20px auto; padding: 0 10px; background: #1a1a1a; color: #e0e0e0; }}
  h1 {{ color: #7ec8e3; }}
  h2 {{ color: #c0c0c0; border-bottom: 1px solid #333; padding-bottom: 4px; }}
  table {{ border-collapse: collapse; width: 100%; margin-bottom: 20px; }}
  th, td {{ border: 1px solid #333; padding: 6px 8px; text-align: left; font-size: 13px; }}
  th {{ background: #2a2a2a; color: #7ec8e3; }}
  tr:hover {{ background: #2a2a2a; }}
  .status-pending {{ color: #f0ad4e; }}
  .status-sent {{ color: #5bc0de; }}
  .status-active {{ color: #5cb85c; }}
  .status-done, .status-dismissed {{ color: #777; }}
  .btn {{ background: #c9302c; color: white; border: none; padding: 3px 8px; cursor: pointer; font-family: monospace; font-size: 12px; }}
  .btn:hover {{ background: #ac2925; }}
  .btn-cleanup {{ background: #555; color: #ccc; border: 1px solid #666; padding: 5px 12px; cursor: pointer; font-family: monospace; font-size: 12px; margin-bottom: 10px; }}
  .btn-cleanup:hover {{ background: #c9302c; color: white; border-color: #c9302c; }}
  nav {{ margin-bottom: 16px; }}
  nav a {{ color: #7ec8e3; margin-right: 12px; }}
  .empty {{ color: #666; font-style: italic; }}
</style>
</head>
<body>
<h1>ADHD Bot</h1>
<nav><a href="/">Reminders</a> <a href="/recurring">Recurring</a> <a href="/actions">Action Items</a></nav>
{body}
</body>
</html>"""
    return HTMLResponse(html)


@app.get("/", response_class=HTMLResponse)
def reminders_page():
    db = SessionLocal()
    try:
        rows = db.query(Reminder).order_by(Reminder.fire_at.desc()).all()
        if not rows:
            return _render_page("<h2>Reminders</h2><p class='empty'>No reminders.</p>")

        trs = ""
        for r in rows:
            trs += f"""<tr>
              <td>{r.id}</td>
              <td>{r.label}</td>
              <td>{r.message[:80]}</td>
              <td>{_fmt(r.fire_at)}</td>
              <td class="status-{r.status}">{r.status}</td>
              <td>{_fmt(r.sent_at)}</td>
              <td><form method="post" action="/delete/reminder/{r.id}" style="margin:0">
                <button class="btn" onclick="return confirm('Delete?')">del</button>
              </form></td>
            </tr>"""

        cleanup_btn = ""
        done_count = sum(1 for r in rows if r.status in ("dismissed", "cancelled", "sent"))
        if done_count:
            cleanup_btn = f"""<form method="post" action="/delete/reminders/completed" style="margin:0;display:inline">
              <button class="btn-cleanup" onclick="return confirm('Delete {done_count} completed/cancelled/sent reminders?')">Delete all completed/cancelled ({done_count})</button>
            </form>"""

        table = f"""<h2>Reminders ({len(rows)})</h2>
        {cleanup_btn}
        <table><tr><th>ID</th><th>Label</th><th>Message</th><th>Fire At</th><th>Status</th><th>Sent</th><th></th></tr>
        {trs}</table>"""
        return _render_page(table)
    finally:
        db.close()


@app.get("/recurring", response_class=HTMLResponse)
def recurring_page():
    db = SessionLocal()
    try:
        rows = db.query(RecurringSchedule).order_by(RecurringSchedule.next_fire_at.desc()).all()
        if not rows:
            return _render_page("<h2>Recurring Schedules</h2><p class='empty'>None.</p>")

        trs = ""
        for r in rows:
            trs += f"""<tr>
              <td>{r.id}</td>
              <td>{r.label}</td>
              <td>{r.cron_expression}</td>
              <td>{_fmt(r.next_fire_at)}</td>
              <td class="status-{r.status}">{r.status}</td>
              <td><form method="post" action="/delete/recurring/{r.id}" style="margin:0">
                <button class="btn" onclick="return confirm('Delete?')">del</button>
              </form></td>
            </tr>"""

        cleanup_btn = ""
        done_count = sum(1 for r in rows if r.status == "deleted")
        if done_count:
            cleanup_btn = f"""<form method="post" action="/delete/recurring/completed" style="margin:0;display:inline">
              <button class="btn-cleanup" onclick="return confirm('Delete {done_count} cancelled recurring schedules?')">Delete all cancelled ({done_count})</button>
            </form>"""

        table = f"""<h2>Recurring Schedules ({len(rows)})</h2>
        {cleanup_btn}
        <table><tr><th>ID</th><th>Label</th><th>Cron</th><th>Next Fire</th><th>Status</th><th></th></tr>
        {trs}</table>"""
        return _render_page(table)
    finally:
        db.close()


@app.get("/actions", response_class=HTMLResponse)
def actions_page():
    db = SessionLocal()
    try:
        rows = db.query(ActionItem).order_by(ActionItem.created_at.desc()).all()
        if not rows:
            return _render_page("<h2>Action Items</h2><p class='empty'>None.</p>")

        trs = ""
        for r in rows:
            trs += f"""<tr>
              <td>{r.id}</td>
              <td>{r.description[:80]}</td>
              <td>{r.source}</td>
              <td class="status-{r.status}">{r.status}</td>
              <td>{r.remind_count}</td>
              <td>{_fmt(r.next_remind_at)}</td>
              <td><form method="post" action="/delete/action/{r.id}" style="margin:0">
                <button class="btn" onclick="return confirm('Delete?')">del</button>
              </form></td>
            </tr>"""

        cleanup_btn = ""
        done_count = sum(1 for r in rows if r.status == "done")
        if done_count:
            cleanup_btn = f"""<form method="post" action="/delete/actions/completed" style="margin:0;display:inline">
              <button class="btn-cleanup" onclick="return confirm('Delete {done_count} completed action items?')">Delete all completed ({done_count})</button>
            </form>"""

        table = f"""<h2>Action Items ({len(rows)})</h2>
        {cleanup_btn}
        <table><tr><th>ID</th><th>Description</th><th>Source</th><th>Status</th><th>Nags</th><th>Next Remind</th><th></th></tr>
        {trs}</table>"""
        return _render_page(table)
    finally:
        db.close()


@app.post("/delete/reminder/{id}")
def delete_reminder(id: int):
    db = SessionLocal()
    try:
        db.query(Reminder).filter(Reminder.id == id).delete()
        db.commit()
    finally:
        db.close()
    return RedirectResponse("/", status_code=303)


@app.post("/delete/recurring/{id}")
def delete_recurring(id: int):
    db = SessionLocal()
    try:
        db.query(RecurringSchedule).filter(RecurringSchedule.id == id).delete()
        db.commit()
    finally:
        db.close()
    return RedirectResponse("/recurring", status_code=303)


@app.post("/delete/action/{id}")
def delete_action(id: int):
    db = SessionLocal()
    try:
        db.query(ActionItem).filter(ActionItem.id == id).delete()
        db.commit()
    finally:
        db.close()
    return RedirectResponse("/actions", status_code=303)


@app.post("/delete/reminders/completed")
def delete_completed_reminders():
    db = SessionLocal()
    try:
        db.query(Reminder).filter(Reminder.status.in_(("dismissed", "cancelled", "sent"))).delete(synchronize_session=False)
        db.commit()
    finally:
        db.close()
    return RedirectResponse("/", status_code=303)


@app.post("/delete/recurring/completed")
def delete_completed_recurring():
    db = SessionLocal()
    try:
        db.query(RecurringSchedule).filter(RecurringSchedule.status == "deleted").delete(synchronize_session=False)
        db.commit()
    finally:
        db.close()
    return RedirectResponse("/recurring", status_code=303)


@app.post("/delete/actions/completed")
def delete_completed_actions():
    db = SessionLocal()
    try:
        db.query(ActionItem).filter(ActionItem.status == "done").delete(synchronize_session=False)
        db.commit()
    finally:
        db.close()
    return RedirectResponse("/actions", status_code=303)


if __name__ == "__main__":
    import uvicorn
    Base.metadata.create_all(engine)
    uvicorn.run(app, host="0.0.0.0", port=8081)
