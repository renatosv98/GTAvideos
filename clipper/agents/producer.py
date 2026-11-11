"""Agent 3 - Producer.

Input:  ranked clip plans from the selector.
Output: finished vertical shorts (face top / gameplay bottom, jump cuts, hook, captions, ducked background music),
        stored as clips in the database with status 'rendered', most viral first.
"""
from __future__ import annotations

import json
from pathlib import Path

from .. import db, music
from ..config import DATA, Creator
from ..render import render_clip


def recent_tracks(n: int = 6) -> list[str]:
    with db.connect() as conn:
        rows = conn.execute("SELECT edit_json FROM clips WHERE edit_json IS NOT NULL ORDER BY id DESC LIMIT ?",
                            (n,)).fetchall()
    return [t for r in rows if (t := json.loads(r["edit_json"]).get("music"))]


def produce(clip_id: int, video: Path, creator: Creator) -> Path:
    """(Re)render one clip from what's stored for it in the database."""
    with db.connect() as conn:
        r = conn.execute("SELECT * FROM clips WHERE id=?", (clip_id,)).fetchone()
    edit = json.loads(r["edit_json"] or "{}")
    track = music.track_file(edit["music"]) if edit.get("music") else None
    return render_clip(video, r["start"], r["end"], json.loads(r["words_json"] or "[]"), creator,
                       DATA / "clips" / f"{clip_id:05d}", cuts=edit.get("cuts"), hook=edit.get("hook"),
                       music=track if track and track.exists() else None)


def run(stream_id: int, video: Path, creator: Creator, picks: list[dict]) -> list[int]:
    ids = []
    for p in picks:
        track = music.pick(p.get("music_mood", "funny"), avoid=recent_tracks())
        edit = {"cuts": p.get("cuts"), "hook": p.get("hook"), "music_mood": p.get("music_mood"),
                "music": track["title"] if track else None}
        with db.connect() as conn:
            clip_id = conn.execute(
                "INSERT INTO clips (stream_id, start, end, score, title, caption, hashtags, reason, risk_notes,"
                " words_json, edit_json) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (stream_id, p["start"], p["end"], p["viral_chance"], p["title"], p["caption"],
                 " ".join(h if h.startswith("#") else f"#{h}" for h in p["hashtags"]),
                 p["reason"], p["risk_notes"], json.dumps(p["words"]), json.dumps(edit, ensure_ascii=False)),
            ).lastrowid
        try:
            path, status, err = produce(clip_id, video, creator), "rendered", None
        except Exception as e:  # keep going with the other clips
            path, status, err = None, "failed", str(e)
            print(f"    clip {clip_id} failed: {e}")
        with db.connect() as conn:
            conn.execute("UPDATE clips SET path=?, status=?, review_notes=? WHERE id=?",
                         (str(path) if path else None, status, err, clip_id))
        ids.append(clip_id)
        print(f"    #{clip_id} [{p['viral_chance']}% viral chance, music: {edit['music'] or 'none'}] {p['title']}")
    return ids
