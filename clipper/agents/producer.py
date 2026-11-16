"""Agent 3 - Producer.

Input:  ranked clip plans from the selector.
Output: finished vertical shorts (face top / gameplay bottom, jump cuts, hook, captions, ducked background music),
        stored as clips in the database with status 'rendered', most viral first.
"""
from __future__ import annotations

import json
from datetime import date
from pathlib import Path

from .. import db, music
from ..config import DATA, SETTINGS, Creator
from ..scheduler import assign_slot, reprioritize
from ..render import render_clip
from . import facecam, guidance, qa


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
    if "facecam" not in edit:  # streamers move their camera between scenes: find it for this clip
        try:
            edit["facecam"] = facecam.detect(video, r["start"], r["end"])
            print(f"    #{clip_id} camera: {edit['facecam'] or 'none (no face panel)'}")
        except Exception as e:  # fall back to the creator's usual position
            print(f"    #{clip_id} camera detection failed ({e}); using creators.yaml position")
            edit["facecam"] = None
        with db.connect() as conn:
            conn.execute("UPDATE clips SET edit_json=? WHERE id=?", (json.dumps(edit, ensure_ascii=False), clip_id))
    track = music.track_file(edit["music"]) if edit.get("music") else None
    return render_clip(video, r["start"], r["end"], json.loads(r["words_json"] or "[]"), creator,
                       DATA / "clips" / f"{clip_id:05d}", cuts=edit.get("cuts"), hook=edit.get("hook"),
                       music=track if track and track.exists() else None, facecam=edit.get("facecam"))


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
        if path:
            check_and_approve(clip_id, creator)
    return ids


def auto_approve_active() -> bool:
    until = SETTINGS.get("autopilot", {}).get("auto_approve_until")
    if str(until).lower() == "always":
        return True
    return bool(until) and date.today() <= date.fromisoformat(str(until))


def check_and_approve(clip_id: int, creator: Creator) -> str:
    """QA the finished short; failures go back to the selector as feedback. Passing clips are auto-approved
    while auto-approve is on (week 1), otherwise they wait for you in `clipper review`."""
    cfg = SETTINGS.get("autopilot", {})
    with db.connect() as conn:
        clip = dict(conn.execute("SELECT * FROM clips WHERE id=?", (clip_id,)).fetchone())
        existing = [r["title"] for r in conn.execute(
            "SELECT title FROM clips WHERE id != ? AND status IN ('approved','published') ORDER BY id DESC LIMIT 40",
            (clip_id,))]
    edit = json.loads(clip["edit_json"] or "{}")
    words = json.loads(clip["words_json"] or "[]")
    try:
        r = qa.review(Path(clip["path"]), {**clip, "hook": edit.get("hook")}, " ".join(w["w"] for w in words),
                      existing, creator.language)
    except Exception as e:
        print(f"    #{clip_id} QA could not run ({e}); left for manual review")
        return "rendered"
    with db.connect() as conn:
        conn.execute("UPDATE clips SET title=?, caption=? WHERE id=?", (r["title"], r["caption"], clip_id))
        if not r["passed"]:
            why = "; ".join(r["issues"]) + (f" (duplicate of: {r['duplicate_of']})" if r["duplicate_of"] else "")
            conn.execute("UPDATE clips SET status='rejected', review_notes=? WHERE id=?", (f"QA: {why}", clip_id))
            conn.execute("INSERT INTO agent_feedback (from_agent, to_agent, clip_id, kind, message) "
                         "VALUES ('producer','selector',?,'qa_fail',?)", (clip_id, f"Rejected '{clip['title']}': {why}"))
            print(f"    #{clip_id} QA ❌ {why}")
            return "rejected"
    if SETTINGS["platforms"]["tiktok"].get("long_cut", True):
        try:
            tiktok_cut(clip_id, creator)
        except Exception as e:
            print(f"    #{clip_id} TikTok cut failed ({e}); TikTok gets the short version")
    try:  # growth agent writes per-platform title/description/hashtags before anything is published
        from . import growth
        growth.package(clip_id)
    except Exception as e:
        print(f"    #{clip_id} packaging failed ({e}); publisher will use the selector's text")
    with db.connect() as conn:
        if auto_approve_active() and (clip["score"] or 0) >= cfg.get("min_viral_chance", 30):
            conn.execute("UPDATE clips SET status='approved', review_notes='auto-approved (QA passed)', slot_at=? "
                         "WHERE id=?", (assign_slot(conn), clip_id))
            reprioritize(conn)
            print(f"    #{clip_id} QA ✅ auto-approved")
            return "approved"
    print(f"    #{clip_id} QA ✅ waiting for your review")
    return "rendered"


# ------------------------------------------------------------ 60-90 s TikTok cut (Creator Rewards pays only 1 min+)

TIKTOK_PLAN = {
    "type": "object",
    "properties": {
        "worth_it": {"type": "boolean"},
        "keep_segments": {"type": "array", "items": {"type": "array", "items": {"type": "number"}}},
        "reason": {"type": "string"},
    },
    "required": ["worth_it", "keep_segments", "reason"], "additionalProperties": False,
}


def tiktok_cut(clip_id: int, creator: Creator) -> Path | None:
    """Render a 61-90 s version of the clip for TikTok (more setup/context, same payoff), if the moment supports it
    without padding. Saved next to the short as tiktok.mp4; the publisher uses it for TikTok only."""
    from .. import llm
    from .transcriber import phrases, words_between
    with db.connect() as conn:
        r = dict(conn.execute("SELECT c.*, s.video_path FROM clips c JOIN streams s ON s.id=c.stream_id "
                              "WHERE c.id=?", (clip_id,)).fetchone())
    if not r["video_path"] or not Path(r["video_path"]).exists():
        return None
    report = Path(r["video_path"]).parent / "report.json"
    if not report.exists():
        return None
    words = json.loads(report.read_text())["words"]
    edit = json.loads(r["edit_json"] or "{}")
    a, b = max(0.0, r["start"] - 75), r["end"] + 30
    lines = "\n".join(f"[{s:.1f}-{e:.1f}] {t}" for s, e, t in phrases(words_between(words, a, b)))
    plan = llm.ask_json(
        "You are a short-form video editor making the TikTok version of a clip: 61-90 seconds of KEPT footage "
        "(TikTok's Creator Rewards only pays on videos of 1 minute or more)." + guidance.brief("producer"), (
            f"The short version keeps {edit.get('cuts') or [[r['start'], r['end']]]} (absolute seconds) and is about: "
            f"{r['title']} (hook: {edit.get('hook')}).\nTranscript around it:\n{lines}\n\n"
            "Plan the TikTok cut: keep_segments in order (absolute seconds, inside the transcript range) totalling "
            "61-90 s. Add setup/context BEFORE the payoff and a longer reaction after, cut dead air, keep it a story; "
            "the first seconds must still hook. If reaching 61 s needs boring padding, set worth_it=false."),
        TIKTOK_PLAN, effort="low")
    segs = [s for s in plan["keep_segments"] if len(s) == 2 and s[1] > s[0]]
    total = sum(e - s for s, e in segs)
    if not plan["worth_it"] or not 61 <= total <= 95:
        print(f"    #{clip_id} no TikTok long cut ({plan['reason'][:100]})")
        return None
    track = music.track_file(edit["music"]) if edit.get("music") else None
    build = DATA / "clips" / f"{clip_id:05d}" / "tiktok_build"
    out = render_clip(Path(r["video_path"]), segs[0][0], segs[-1][1], words_between(words, segs[0][0], segs[-1][1]),
                      creator, build, cuts=segs, hook=edit.get("hook"),
                      music=track if track and track.exists() else None, facecam=edit.get("facecam"))
    final = DATA / "clips" / f"{clip_id:05d}" / "tiktok.mp4"
    out.replace(final)
    print(f"    #{clip_id} TikTok cut: {total:.0f} s")
    return final
