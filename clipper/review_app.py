"""Local review UI: watch each short, edit the text, tick the copyright checklist, approve or reject."""
from __future__ import annotations

from pathlib import Path

from flask import Flask, abort, redirect, render_template_string, request, send_file, url_for

from . import db
from .config import load_creators
from .scheduler import assign_slot, fmt_local, reprioritize

app = Flask(__name__)

CHECKS = [
    ("permission", "The creator is on the allowlist and this clip fits their clipping terms"),
    ("credit", "The creator credit is visible and correct"),
    ("music", "No licensed music audible (in-game radio, Spotify or stream background music)"),
    ("third_party", "No other people's content on screen (videos, TikToks, other streamers' footage)"),
    ("transformative", "The bottom footage is mine or licensed, and captions/edit add value"),
    ("safe", "No slurs, doxxing, or anything against platform rules"),
]

PAGE = """<!doctype html><html><head><meta charset="utf-8"><title>Clip review</title>
<meta name="viewport" content="width=device-width,initial-scale=1">
<style>
:root{--bg:#0f1115;--card:#181b22;--fg:#e8e8ea;--mut:#9aa0aa;--ok:#2fbf71;--bad:#e5484d;--warn:#f5a524}
body{background:var(--bg);color:var(--fg);font:15px/1.45 -apple-system,system-ui,sans-serif;margin:0;padding:16px}
h1{font-size:20px} nav a{color:var(--mut);margin-right:14px} nav a.on{color:var(--fg);font-weight:600}
.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(340px,1fr));gap:16px}
.card{background:var(--card);border-radius:12px;padding:14px}
video{width:100%;aspect-ratio:9/16;background:#000;border-radius:8px}
input[type=text],textarea{width:100%;box-sizing:border-box;background:#0f1115;color:var(--fg);border:1px solid #2a2f3a;border-radius:6px;padding:6px}
label{display:block;margin:4px 0;color:var(--mut)} .risk{color:var(--warn)} .meta{color:var(--mut);font-size:13px}
button{border:0;border-radius:6px;padding:8px 14px;font-weight:600;cursor:pointer;margin-right:6px}
.ok{background:var(--ok);color:#000}.bad{background:var(--bad);color:#fff}
</style></head><body>
<h1>GTA 6 shorts: review queue</h1>
<nav>{% for s in ['rendered','approved','published','rejected','failed'] %}<a class="{{'on' if s==status}}" href="?status={{s}}">{{s}} ({{counts.get(s,0)}})</a>{% endfor %}</nav><br>
<div class="grid">
{% for c in clips %}
<form class="card" method="post" action="{{ url_for('decide', clip_id=c.id) }}">
  {% if c.path %}<video src="{{ url_for('video', clip_id=c.id) }}" controls preload="metadata"></video>{% endif %}
  <div class="meta">#{{c.id}} · {{creators.get(c.creator_id, c.creator_id)}} · {{'%.0f'|format(c.end-c.start)}}s · <b>{{c.score|int if c.score is not none else '?'}}% viral chance</b>
   {% if c.slot_at %}· slot {{ fmt_local(c.slot_at) }}{% endif %}</div>
  <p class="meta">{{c.reason}}</p>
  <p class="risk">⚠ Risk: {{c.risk_notes}}</p>
  {% if c.review_notes %}<p class="meta">Notes: {{c.review_notes}}</p>{% endif %}
  <label>Title <input type="text" name="title" value="{{c.title}}"></label>
  <label>Caption <textarea name="caption" rows="2">{{c.caption}}</textarea></label>
  <label>Hashtags <input type="text" name="hashtags" value="{{c.hashtags}}"></label>
  {% if status == 'rendered' %}
    {% for key, text in checks %}<label><input type="checkbox" name="chk_{{key}}" required> {{text}}</label>{% endfor %}
    <br><button class="ok" name="action" value="approve">Approve + schedule</button>
    <button class="bad" name="action" value="reject" formnovalidate>Reject</button>
  {% elif status == 'approved' %}
    <button name="action" value="save">Save text</button>
    <button class="bad" name="action" value="unapprove" formnovalidate>Pull from schedule</button>
  {% endif %}
</form>
{% else %}<p class="meta">Nothing here.</p>{% endfor %}
</div></body></html>"""


@app.get("/")
def index():
    status = request.args.get("status", "rendered")
    with db.connect() as conn:
        clips = conn.execute(
            "SELECT clips.*, streams.creator_id FROM clips JOIN streams ON streams.id = clips.stream_id "
            "WHERE clips.status = ? ORDER BY COALESCE(slot_at, ''), score DESC NULLS LAST, clips.id", (status,)
        ).fetchall()
        counts = dict(conn.execute("SELECT status, COUNT(*) FROM clips GROUP BY status").fetchall())
    creators = {c.id: c.display_name for c in load_creators()}
    return render_template_string(PAGE, clips=clips, status=status, counts=counts, checks=CHECKS,
                                  creators=creators, fmt_local=fmt_local)


@app.get("/video/<int:clip_id>")
def video(clip_id: int):
    with db.connect() as conn:
        row = conn.execute("SELECT path FROM clips WHERE id = ?", (clip_id,)).fetchone()
    if not row or not row["path"] or not Path(row["path"]).exists():
        abort(404)
    return send_file(row["path"], mimetype="video/mp4", conditional=True)


@app.post("/clip/<int:clip_id>")
def decide(clip_id: int):
    f = request.form
    action = f.get("action")
    with db.connect() as conn:
        conn.execute("UPDATE clips SET title=?, caption=?, hashtags=? WHERE id=?",
                     (f.get("title"), f.get("caption"), f.get("hashtags"), clip_id))
        if action == "approve":
            if not all(f.get(f"chk_{k}") for k, _ in CHECKS):
                abort(400, "Every checklist item must be ticked to approve.")
            conn.execute("UPDATE clips SET status='approved', slot_at=? WHERE id=?", (assign_slot(conn), clip_id))
            reprioritize(conn)
        elif action == "reject":
            conn.execute("UPDATE clips SET status='rejected', slot_at=NULL WHERE id=?", (clip_id,))
        elif action == "unapprove":
            conn.execute("UPDATE clips SET status='rendered', slot_at=NULL WHERE id=?", (clip_id,))
    return redirect(url_for("index", status=request.args.get("from", "rendered" if action in ("approve", "reject") else "approved")))


def run(port: int = 5050) -> None:
    print(f"Review UI: http://127.0.0.1:{port}")
    app.run(host="127.0.0.1", port=port, debug=False)
