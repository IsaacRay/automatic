"""Bare-bones web UI for viewing and deleting reminders. Runs on port 8081."""

from datetime import datetime, timezone

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from app.database import engine, Base, SessionLocal
from app.models import Reminder, NagSchedule, ExerciseLog
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
<nav><a href="/">Reminders</a> <a href="/nags">Nags</a> <a href="/exercise">Exercise</a></nav>
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
            cron = r.cron_expression or "-"
            trs += f"""<tr>
              <td>{r.id}</td>
              <td>{r.label}</td>
              <td>{r.message[:80]}</td>
              <td>{cron}</td>
              <td>{_fmt(r.fire_at)}</td>
              <td class="status-{r.status}">{r.status}</td>
              <td>{_fmt(r.sent_at)}</td>
              <td><form method="post" action="/delete/reminder/{r.id}" style="margin:0">
                <button class="btn" onclick="return confirm('Delete?')">del</button>
              </form></td>
            </tr>"""

        cleanup_btn = ""
        done_count = sum(1 for r in rows if r.status in ("dismissed", "cancelled", "sent") and not r.cron_expression)
        if done_count:
            cleanup_btn = f"""<form method="post" action="/delete/reminders/completed" style="margin:0;display:inline">
              <button class="btn-cleanup" onclick="return confirm('Delete {done_count} completed/cancelled/sent reminders?')">Delete all completed/cancelled ({done_count})</button>
            </form>"""

        table = f"""<h2>Reminders ({len(rows)})</h2>
        {cleanup_btn}
        <table><tr><th>ID</th><th>Label</th><th>Message</th><th>Cron</th><th>Next Fire</th><th>Status</th><th>Sent</th><th></th></tr>
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


@app.post("/delete/reminders/completed")
def delete_completed_reminders():
    db = SessionLocal()
    try:
        # Only clean up non-recurring sent/dismissed/cancelled reminders
        db.query(Reminder).filter(
            Reminder.status.in_(("dismissed", "cancelled", "sent")),
            Reminder.cron_expression == None,
        ).delete(synchronize_session=False)
        db.commit()
    finally:
        db.close()
    return RedirectResponse("/", status_code=303)


@app.get("/nags", response_class=HTMLResponse)
def nags_page():
    db = SessionLocal()
    try:
        rows = db.query(NagSchedule).order_by(NagSchedule.next_nag_at.desc()).all()
        if not rows:
            return _render_page("<h2>Nag Schedules</h2><p class='empty'>None.</p>")

        trs = ""
        for r in rows:
            active = "ACTIVE" if r.active_since else "-"
            dur = f"{r.max_duration_minutes}m" if r.max_duration_minutes else "∞"
            anchor = ""
            if r.anchor_to_completion:
                period = f"{r.cycle_months}mo" if r.cycle_months else f"{r.cycle_days}d"
                anchor = f"&#x2693; {period}"
            repeating = r.recurrence_description if r.recurrence_description else ("Yes" if r.repeating else "No")
            source = r.source or "-"
            trs += f"""<tr>
              <td>{r.id}</td>
              <td>{r.label}</td>
              <td>{source}</td>
              <td>{r.cron_expression}</td>
              <td>{r.interval_minutes}m</td>
              <td>{dur}</td>
              <td>{repeating}</td>
              <td>{_fmt(r.next_nag_at)}</td>
              <td class="status-{r.status}">{r.status}</td>
              <td>{active}</td>
              <td>{anchor}</td>
              <td><form method="post" action="/delete/nag/{r.id}" style="margin:0">
                <button class="btn" onclick="return confirm('Delete?')">del</button>
              </form></td>
            </tr>"""

        cleanup_btn = ""
        done_count = sum(1 for r in rows if r.status == "deleted")
        if done_count:
            cleanup_btn = f"""<form method="post" action="/delete/nags/completed" style="margin:0;display:inline">
              <button class="btn-cleanup" onclick="return confirm('Delete {done_count} cancelled nag schedules?')">Delete all cancelled ({done_count})</button>
            </form>"""

        table = f"""<h2>Nags ({len(rows)})</h2>
        {cleanup_btn}
        <table><tr><th>ID</th><th>Label</th><th>Source</th><th>Cron</th><th>Interval</th><th>Duration</th><th>Repeating</th><th>Next Nag</th><th>Status</th><th>Active</th><th>Anchor</th><th></th></tr>
        {trs}</table>"""
        return _render_page(table)
    finally:
        db.close()


@app.post("/delete/nag/{id}")
def delete_nag(id: int):
    db = SessionLocal()
    try:
        db.query(NagSchedule).filter(NagSchedule.id == id).delete()
        db.commit()
    finally:
        db.close()
    return RedirectResponse("/nags", status_code=303)


@app.get("/exercise", response_class=HTMLResponse)
def exercise_page():
    db = SessionLocal()
    try:
        rows = db.query(ExerciseLog).order_by(ExerciseLog.created_at.desc()).all()
        if not rows:
            return _render_page("<h2>Exercise Log</h2><p class='empty'>No activities logged.</p>")

        trs = ""
        for r in rows:
            dist = f"{r.distance_miles} mi" if r.distance_miles else "-"
            dur = f"{r.duration_minutes} min" if r.duration_minutes else "-"
            notes = (r.notes[:60] + "...") if r.notes and len(r.notes) > 60 else (r.notes or "-")
            trs += f"""<tr>
              <td>{r.id}</td>
              <td>{_fmt(r.created_at)}</td>
              <td>{r.activity}</td>
              <td>{dist}</td>
              <td>{dur}</td>
              <td>{notes}</td>
              <td><form method="post" action="/delete/exercise/{r.id}" style="margin:0">
                <button class="btn" onclick="return confirm('Delete?')">del</button>
              </form></td>
            </tr>"""

        cleanup_btn = ""
        if len(rows) > 1:
            cleanup_btn = f"""<form method="post" action="/delete/exercise/all" style="margin:0;display:inline">
              <button class="btn-cleanup" onclick="return confirm('Delete all {len(rows)} exercise log entries?')">Delete all ({len(rows)})</button>
            </form>"""

        table = f"""<h2>Exercise Log ({len(rows)})</h2>
        {cleanup_btn}
        <table><tr><th>ID</th><th>Date</th><th>Activity</th><th>Distance</th><th>Duration</th><th>Notes</th><th></th></tr>
        {trs}</table>"""
        return _render_page(table)
    finally:
        db.close()


@app.post("/delete/exercise/{id}")
def delete_exercise(id: int):
    db = SessionLocal()
    try:
        db.query(ExerciseLog).filter(ExerciseLog.id == id).delete()
        db.commit()
    finally:
        db.close()
    return RedirectResponse("/exercise", status_code=303)


@app.post("/delete/exercise/all")
def delete_all_exercise():
    db = SessionLocal()
    try:
        db.query(ExerciseLog).delete(synchronize_session=False)
        db.commit()
    finally:
        db.close()
    return RedirectResponse("/exercise", status_code=303)


@app.post("/delete/nags/completed")
def delete_completed_nags():
    db = SessionLocal()
    try:
        db.query(NagSchedule).filter(NagSchedule.status == "deleted").delete(synchronize_session=False)
        db.commit()
    finally:
        db.close()
    return RedirectResponse("/nags", status_code=303)


if __name__ == "__main__":
    import uvicorn
    Base.metadata.create_all(engine)
    uvicorn.run(app, host="0.0.0.0", port=8081)
