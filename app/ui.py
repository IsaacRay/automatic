"""Bare-bones web UI for viewing and deleting reminders. Runs on port 8081."""

from datetime import datetime, timezone

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from app.database import engine, Base, SessionLocal
from app.models import NagSchedule, CheckList, CheckListItem
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
  .checklist {{ list-style: none; padding: 0; }}
  .checklist li {{ display: flex; align-items: center; padding: 8px 6px; border-bottom: 1px solid #2a2a2a; }}
  .checklist li:hover {{ background: #2a2a2a; }}
  .checklist input[type=checkbox] {{ width: 20px; height: 20px; margin-right: 12px; cursor: pointer; }}
  .checklist .label {{ flex: 1; font-size: 15px; }}
  .checklist .done .label {{ color: #777; text-decoration: line-through; }}
  .checklist .item-body {{ flex: 1; display: flex; flex-direction: column; }}
  .checklist li.with-meta {{ align-items: flex-start; }}
  .checklist .meta {{ color: #888; font-size: 12px; margin-top: 2px; margin-left: 4px; }}
  .checklist form {{ margin: 0; }}
  .hint {{ color: #888; font-size: 12px; margin-bottom: 12px; }}
</style>
</head>
<body>
<h1>ADHD Bot</h1>
<nav><a href="/">Today</a> <a href="/lists">Lists</a> <a href="/nags">Nags</a></nav>
{body}
</body>
</html>"""
    return HTMLResponse(html)


def _render_today_nags(db) -> str:
    """Render the today list: active nags due/scheduled for today, with check-off."""
    from app.context_engine import today_items, is_done_today, cycle_deadline
    now = datetime.now(timezone.utc)
    nags = today_items(db, now)
    # Open items first (by effective deadline), checked-off items sink to the bottom.
    nags.sort(key=lambda n: (is_done_today(n, now), cycle_deadline(n, now) or n.next_nag_at))
    hint = "<p class='hint'>Text \".. &lt;thing&gt;\" to add an item. Reply \"&lt;thing&gt; done\" to check off.</p>"
    if not nags:
        return f"<h2>Today's List</h2>{hint}<p class='empty'>Nothing on the list today.</p>"

    lis = ""
    for n in nags:
        done = is_done_today(n, now)
        label = _escape(n.label)
        cd = cycle_deadline(n, now)
        if cd:
            deadline = _fmt(cd)
        elif n.repeating:
            deadline = "11:00 PM"
        else:
            deadline = "-"
        nextnag = _fmt(n.next_nag_at)
        sc = n.snooze_count or 0
        badge_color = "#d9534f" if sc > 2 else "#f0ad4e"
        snooze_badge = (
            f"<span title='Snoozed {sc} time{'s' if sc != 1 else ''}' "
            f"style=\"background:{badge_color};color:#fff;font-size:11px;font-weight:bold;"
            f"border-radius:10px;padding:1px 7px;margin-left:6px\">💤 {sc}</span>"
            if sc else ""
        )
        checked = "checked" if done else ""
        done_class = " done" if done else ""
        lis += f"""<li class="with-meta{done_class}">
          <form method="post" action="/nag/done/{n.id}">
            <input type="checkbox" {checked} onchange="this.form.submit()">
          </form>
          <div class="item-body">
            <div><span class="label">{label}</span>{snooze_badge}</div>
            <div class="meta">due {deadline} · next {nextnag}</div>
          </div>
        </li>"""
    return f"""<h2>Today's List ({len(nags)})</h2>{hint}<ul class="checklist">{lis}</ul>"""


@app.post("/nag/done/{id}")
def nag_done(id: int):
    """Toggle a today-list item: check off if open, re-open if already done today."""
    db = SessionLocal()
    try:
        from app.intent_router import execute_acknowledge, reopen_nag
        from app.context_engine import is_done_today
        now = datetime.now(timezone.utc)
        nag = db.query(NagSchedule).filter(NagSchedule.id == id).first()
        if nag and is_done_today(nag, now):
            reopen_nag(db, id)
        else:
            execute_acknowledge(db, {"matched_id": id, "matched_type": "nag"})
    finally:
        db.close()
    return RedirectResponse("/", status_code=303)


@app.get("/", response_class=HTMLResponse)
def checklist_page():
    db = SessionLocal()
    try:
        return _render_page(_render_today_nags(db))
    finally:
        db.close()


def _escape(s: str) -> str:
    return (s or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _render_list_block(lst: CheckList, items, heading_level: int = 2, show_resurrect: bool = False, collapsed: bool = False) -> str:
    """Render a checklist as a Today-style block."""
    title = _escape(lst.title)
    h = f"h{heading_level}"
    if not items:
        body_inner = "<p class='empty'>No items.</p>"
    else:
        lis = ""
        for it in items:
            done = it.completed_at is not None
            checked = "checked" if done else ""
            done_class = "done" if done else ""
            label = _escape(it.label)
            lis += f"""<li class="{done_class}">
              <form method="post" action="/lists/toggle/{it.id}">
                <input type="checkbox" {checked} onchange="this.form.submit()">
              </form>
              <span class="label">{label}</span>
            </li>"""
        body_inner = f"<ul class='checklist'>{lis}</ul>"

    actions = f"""<form method="post" action="/lists/delete/{lst.id}" style="margin:0;display:inline">
      <button class="btn" onclick="return confirm('Delete this list?')">delete</button>
    </form>"""
    if show_resurrect:
        actions = f"""<form method="post" action="/lists/resurrect/{lst.id}" style="margin:0;display:inline;margin-right:6px">
          <button class="btn-cleanup">make current</button>
        </form>""" + actions

    created = _fmt(lst.created_at)
    title_html = f"{title} <span style='color:#666;font-size:13px;font-weight:normal'>({created})</span>"
    if collapsed:
        return (
            f"<details style='margin:8px 0'>"
            f"<summary style='cursor:pointer;font-size:1.17em;font-weight:bold;padding:4px 0'>{title_html}</summary>"
            f"{actions}{body_inner}"
            f"</details>"
        )
    return f"<{h}>{title_html}</{h}>{actions}{body_inner}"


@app.get("/lists", response_class=HTMLResponse)
def lists_page():
    db = SessionLocal()
    try:
        lists = db.query(CheckList).order_by(CheckList.activated_at.desc()).all()
        hint = "<p class='hint'>Text \"#newlist &lt;title&gt;\\nitem1\\nitem2...\" to create a new list.</p>"
        if not lists:
            return _render_page(f"<h2>Lists</h2>{hint}<p class='empty'>No lists yet.</p>")

        items_by_list = {}
        for lst in lists:
            items_by_list[lst.id] = db.query(CheckListItem).filter(
                CheckListItem.checklist_id == lst.id
            ).order_by(CheckListItem.position.asc(), CheckListItem.id.asc()).all()

        current = lists[0]
        body = f"<h2>Current List</h2>{hint}"
        body += _render_list_block(current, items_by_list[current.id], heading_level=3, show_resurrect=False)

        if len(lists) > 1:
            body += "<h2>Previous Lists</h2>"
            for lst in lists[1:]:
                body += _render_list_block(lst, items_by_list[lst.id], heading_level=3, show_resurrect=True, collapsed=True)

        return _render_page(body)
    finally:
        db.close()


@app.post("/lists/toggle/{id}")
def toggle_list_item(id: int):
    db = SessionLocal()
    try:
        item = db.query(CheckListItem).filter(CheckListItem.id == id).first()
        if item:
            item.completed_at = None if item.completed_at else datetime.now(timezone.utc)
            db.commit()
    finally:
        db.close()
    return RedirectResponse("/lists", status_code=303)


@app.post("/lists/delete/{id}")
def delete_list(id: int):
    db = SessionLocal()
    try:
        db.query(CheckListItem).filter(CheckListItem.checklist_id == id).delete()
        db.query(CheckList).filter(CheckList.id == id).delete()
        db.commit()
    finally:
        db.close()
    return RedirectResponse("/lists", status_code=303)


@app.post("/lists/resurrect/{id}")
def resurrect_list(id: int):
    db = SessionLocal()
    try:
        lst = db.query(CheckList).filter(CheckList.id == id).first()
        if lst:
            lst.activated_at = datetime.now(timezone.utc)
            db.commit()
    finally:
        db.close()
    return RedirectResponse("/lists", status_code=303)


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
            anchor = ""
            if r.anchor_to_completion:
                period = f"{r.cycle_months}mo" if r.cycle_months else f"{r.cycle_days}d"
                anchor = f"&#x2693; {period}"
            repeating = r.recurrence_description if r.recurrence_description else ("Yes" if r.repeating else "No")
            source = r.source or "-"
            if r.deadline_at:
                deadline = _fmt(r.deadline_at)
            elif r.deadline_offset_minutes:
                h, m = divmod(r.deadline_offset_minutes, 60)
                deadline = f"T+{h}h{m:02d}m/cycle" if h else f"T+{m}m/cycle"
            else:
                deadline = "-"
            cron_display = r.cron_expression or "-"
            trs += f"""<tr>
              <td>{r.id}</td>
              <td>{r.label}</td>
              <td>{source}</td>
              <td>{cron_display}</td>
              <td>{repeating}</td>
              <td>{deadline}</td>
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
        <table><tr><th>ID</th><th>Label</th><th>Source</th><th>Cron</th><th>Repeating</th><th>Deadline</th><th>Next Nag</th><th>Status</th><th>Active</th><th>Anchor</th><th></th></tr>
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
