"""Agent 5 - Analyst.

Daily: collects views / likes / comments of every published short, compares them with the viral chance the
selector predicted, and rewrites config/playbook.md (the selector reads it before every stream) plus concrete
feedback notes for the selector. Old playbooks are kept in data/playbook_history/.

Data available today: YouTube (views, likes, comments + top comment texts). TikTok stats need the
`video.list` scope (after TikTok approves the app); Instagram after the account is connected.
"""
from __future__ import annotations

import json
import subprocess
from datetime import datetime, timezone

from .guidance import brief
from .. import db, llm
from ..config import DATA, ROOT

PLAYBOOK = ROOT / "config" / "playbook.md"
HISTORY = DATA / "playbook_history"
REPORT = DATA / "analyst_report.md"
LAST = DATA / "analyst_last.txt"
MIN_CLIPS = 5        # need at least this many clips with 24h+ of data before learning anything
MIN_AGE_H = 24

STATS_SCHEMA = """
CREATE TABLE IF NOT EXISTS stats (
    id INTEGER PRIMARY KEY,
    clip_id INTEGER NOT NULL,
    platform TEXT NOT NULL,
    captured_at TEXT DEFAULT (datetime('now')),
    age_hours REAL,
    views INTEGER, likes INTEGER, comments INTEGER
);
"""

SCHEMA = {
    "type": "object",
    "properties": {
        "summary": {"type": "string"},
        "playbook": {"type": "string"},
        "selector_feedback": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["summary", "playbook", "selector_feedback"], "additionalProperties": False,
}

SYSTEM = """You are the analyst of a GTA shorts channel (YouTube Shorts, later TikTok/Instagram).
Your job: find out which shorts get views and why, and turn that into rules the clip selector follows.
Be concrete and evidence-based (cite clip numbers and numbers), separate solid patterns from hunches,
and keep the playbook short: it is read before every stream. Views are the goal metric."""


def _youtube() -> dict[int, dict]:
    from googleapiclient.discovery import build

    from ..uploaders import youtube
    with db.connect() as conn:
        ups = conn.execute("SELECT u.clip_id, u.remote_id, c.slot_at FROM uploads u JOIN clips c ON c.id=u.clip_id "
                           "WHERE u.platform='youtube' AND u.status IN ('scheduled','published') "
                           "AND u.remote_id IS NOT NULL").fetchall()
    ups = [u for u in ups if u["slot_at"] and datetime.fromisoformat(u["slot_at"]) < datetime.now(timezone.utc)]
    if not ups:
        return {}
    yt = build("youtube", "v3", credentials=youtube._creds(), cache_discovery=False)
    by_vid = {u["remote_id"]: u for u in ups}
    out: dict[int, dict] = {}
    ids = list(by_vid)
    for i in range(0, len(ids), 50):
        for v in yt.videos().list(part="statistics", id=",".join(ids[i:i + 50])).execute().get("items", []):
            u, s = by_vid[v["id"]], v["statistics"]
            age = (datetime.now(timezone.utc) - datetime.fromisoformat(u["slot_at"])).total_seconds() / 3600
            out[u["clip_id"]] = {"views": int(s.get("viewCount", 0)), "likes": int(s.get("likeCount", 0)),
                                 "comments": int(s.get("commentCount", 0)), "age_hours": round(age, 1),
                                 "video_id": v["id"], "top_comments": []}
    for cid, d in out.items():
        if d["comments"]:
            try:
                r = yt.commentThreads().list(part="snippet", videoId=d["video_id"], maxResults=10,
                                             order="relevance", textFormat="plainText").execute()
                d["top_comments"] = [t["snippet"]["topLevelComment"]["snippet"]["textDisplay"][:200]
                                     for t in r.get("items", [])]
            except Exception:
                pass
    return out


def _instagram() -> dict[int, dict]:
    import requests

    from ..uploaders import instagram as ig
    ups = _q_uploads("instagram")
    if not ups:
        return {}
    tok = ig._token()
    out = {}
    for u in ups:
        base = f"{ig._host()}/{ig._v()}/{u['remote_id']}"
        d = requests.get(base, params={"fields": "like_count,comments_count", "access_token": tok["access_token"]},
                         timeout=30).json()
        views = None
        for metric in ("views", "plays", "reach"):  # metric names differ between API versions
            r = requests.get(f"{base}/insights", params={"metric": metric, "access_token": tok["access_token"]},
                             timeout=30).json()
            if r.get("data"):
                views = r["data"][0]["values"][0]["value"]
                break
        out[u["clip_id"]] = {"views": views, "likes": d.get("like_count"), "comments": d.get("comments_count")}
    if out and all(v["views"] is None for v in out.values()):
        MISSING.append("instagram views: token lacks instagram_manage_insights (likes/comments still counted)")
    return out


def _tiktok() -> dict[int, dict]:
    """Needs the video.list scope (add it in the TikTok developer portal, then `clipper auth tiktok`)."""
    import requests

    from ..uploaders import tiktok
    ups = _q_uploads("tiktok")
    if not ups:
        return {}
    token = tiktok._token()
    ids = {}
    for u in ups:  # our upload id -> TikTok's public video id
        try:
            st = tiktok._post("/post/publish/status/fetch/", token, {"publish_id": u["remote_id"]})
            for vid in st.get("publicaly_available_post_id") or []:
                ids[str(vid)] = u["clip_id"]
        except Exception:
            pass
    if not ids:
        return {}
    r = requests.post(f"{tiktok.API}/video/query/?fields=id,view_count,like_count,comment_count,share_count",
                      headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
                      json={"filters": {"video_ids": list(ids)[:20]}}, timeout=30).json()
    if r.get("error", {}).get("code", "ok") != "ok":
        raise RuntimeError(f"TikTok stats unavailable ({r['error'].get('code')}): needs the video.list scope")
    return {ids[str(v["id"])]: {"views": v.get("view_count"), "likes": v.get("like_count"),
                                "comments": v.get("comment_count"), "shares": v.get("share_count")}
            for v in r.get("data", {}).get("videos", [])}


def _q_uploads(platform: str) -> list[dict]:
    with db.connect() as conn:
        return [dict(r) for r in conn.execute(
            "SELECT clip_id, remote_id FROM uploads WHERE platform=? AND status='published' AND remote_id IS NOT NULL",
            (platform,))]


MISSING: list[str] = []  # platforms whose stats couldn't be read this run (shown to the analyst)


def collect() -> dict[int, dict]:
    stats = _youtube()
    for d in stats.values():
        d["by_platform"] = {"youtube": {"views": d["views"], "likes": d["likes"], "comments": d["comments"]}}
    MISSING.clear()
    for name, fn in (("instagram", _instagram), ("tiktok", _tiktok)):
        try:
            for cid, d in fn().items():
                if cid in stats:
                    stats[cid]["by_platform"][name] = d
        except Exception as e:
            MISSING.append(f"{name}: {str(e)[:150]}")
    with db.connect() as conn:
        conn.executescript(STATS_SCHEMA)
        for cid, d in stats.items():
            for plat, x in d["by_platform"].items():
                conn.execute("INSERT INTO stats (clip_id, platform, age_hours, views, likes, comments) "
                             "VALUES (?,?,?,?,?,?)", (cid, plat, d["age_hours"], x.get("views"), x.get("likes"),
                                                      x.get("comments")))
            d["views"] = sum((x.get("views") or 0) for x in d["by_platform"].values())  # all platforms
    return stats


def _table(stats: dict[int, dict]) -> str:
    with db.connect() as conn:
        rows = {r["id"]: dict(r) for r in conn.execute(
            "SELECT c.id, c.title, c.score, c.reason, c.edit_json, c.words_json, s.creator_id FROM clips c "
            "JOIN streams s ON s.id=c.stream_id WHERE c.id IN (%s)" % ",".join("?" * len(stats)), list(stats))}
    lines = []
    for cid, d in sorted(stats.items(), key=lambda kv: -kv[1]["views"]):
        c = rows.get(cid)
        if not c:
            continue
        edit = json.loads(c["edit_json"] or "{}")
        words = json.loads(c["words_json"] or "[]")
        kept = sum(b - a for a, b in edit["cuts"]) if edit.get("cuts") else None
        lines.append(json.dumps({
            "clip": cid, "creator": c["creator_id"], "title": c["title"], "hook": edit.get("hook"),
            "predicted_viral_chance": c["score"], "selector_reason": c["reason"],
            "seconds": round(kept, 1) if kept else None, "music_mood": edit.get("music_mood"),
            "age_hours": d["age_hours"], "views_all_platforms": d["views"], "by_platform": d.get("by_platform"),
            "views_per_hour": round(d["views"] / max(d["age_hours"], 1), 2),
            "first_words": " ".join(w["w"] for w in words[:25]),
            "top_comments": d["top_comments"][:5],
        }, ensure_ascii=False))
    return "\n".join(lines)


def run(force: bool = False) -> int:
    print("agent 5 · analyst: collecting stats ...")
    stats = collect()
    mature = {k: v for k, v in stats.items() if v["age_hours"] >= MIN_AGE_H}
    print(f"    {len(stats)} published shorts, {len(mature)} with {MIN_AGE_H}h+ of data")
    LAST.write_text(datetime.now(timezone.utc).isoformat())
    if len(mature) < MIN_CLIPS and not force:
        print(f"    need {MIN_CLIPS} to learn from; playbook unchanged")
        return 0

    with db.connect() as conn:
        human = [r["message"] for r in conn.execute(
            "SELECT message FROM agent_feedback WHERE from_agent IN ('reviewer','user') ORDER BY id DESC LIMIT 20")]
    current = PLAYBOOK.read_text() if PLAYBOOK.exists() else "(empty)"
    r = llm.ask_json(SYSTEM + brief("analyst"), (
        f"# Current playbook\n{current}\n\n"
        f"# Owner's notes on clips (always respect these)\n" + ("\n".join(f"- {m}" for m in human) or "(none)") +
        f"\n\n# Performance of every published short (one JSON per line, best first; views per platform in "
        f"by_platform)\n{_table(stats)}\n\nStats not available this run: {'; '.join(MISSING) or 'none'}\n\n"
        "Tasks:\n"
        "1. summary: 3-5 sentences for the channel owner: what worked, what didn't, how well the predicted viral "
        "chance matched reality.\n"
        "2. playbook: the full new config/playbook.md (markdown, max ~60 lines). Keep owner rules; keep, sharpen "
        "or drop old hypotheses based on the data; add new patterns with the evidence (clip numbers). Include a "
        "short 'What gets views' list and a 'Avoid' list.\n"
        "3. selector_feedback: 2-6 concrete instructions for the clip selector for the next streams."),
        SCHEMA)

    HISTORY.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y-%m-%d_%H%M")
    if PLAYBOOK.exists():
        (HISTORY / f"{stamp}.md").write_text(current)
    PLAYBOOK.write_text(r["playbook"].strip() + "\n")
    with db.connect() as conn:
        for m in r["selector_feedback"]:
            conn.execute("INSERT INTO agent_feedback (from_agent, to_agent, kind, message) "
                         "VALUES ('analyst','selector','performance',?)", (m,))
    REPORT.write_text(f"# Analyst report {stamp}\n\n{r['summary']}\n\n## Feedback to the selector\n"
                      + "\n".join(f"- {m}" for m in r["selector_feedback"]) + "\n")
    print(f"    {r['summary']}\n    playbook updated; report in {REPORT.relative_to(ROOT)}")
    subprocess.run(["osascript", "-e", f"display notification {json.dumps(r['summary'][:220])} "
                                       f"with title \"GTAuploads analyst\""], check=False)
    return 0


def due(hours: float = 20) -> bool:
    try:
        return (datetime.now(timezone.utc) - datetime.fromisoformat(LAST.read_text().strip())
                ).total_seconds() > hours * 3600
    except (OSError, ValueError):
        return True
