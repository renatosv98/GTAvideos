"""Health check: is every clip live on every enabled platform, and will the queue keep posting?

Runs after every publish run (launchd, every 15 min) and sends a macOS notification for each problem,
at most once every `repeat_hours` per problem. `clipper health` prints the full report.
"""
from __future__ import annotations

import json
import subprocess
import time
from datetime import datetime, timedelta, timezone

from . import db
from .config import DATA, SETTINGS

STATE = DATA / "health.json"
REPEAT_HOURS = 6
GRACE = timedelta(hours=1)        # a clip may take this long after its slot to reach every platform
LOW_QUEUE = timedelta(hours=24)   # warn when the last scheduled post is less than this far away


def _enabled() -> list[str]:
    return [p for p, c in SETTINGS["platforms"].items() if c.get("enabled")]


def _youtube_states(ids: list[str]) -> dict[str, dict]:
    """privacy/upload status of our YouTube videos (1 quota unit per 50 videos)."""
    if not ids:
        return {}
    from googleapiclient.discovery import build

    from .uploaders import youtube
    yt = build("youtube", "v3", credentials=youtube._creds(), cache_discovery=False)
    out = {}
    for i in range(0, len(ids), 50):
        for v in yt.videos().list(part="status", id=",".join(ids[i:i + 50])).execute().get("items", []):
            out[v["id"]] = v["status"]
    return out


def check() -> list[tuple[str, str]]:
    """List of (key, message) problems. Empty = all good."""
    now = datetime.now(timezone.utc)
    problems: list[tuple[str, str]] = []
    platforms = _enabled()
    with db.connect() as conn:
        clips = conn.execute(
            "SELECT id, slot_at, title FROM clips WHERE status IN ('approved','published') AND slot_at IS NOT NULL"
        ).fetchall()
        ups = conn.execute("SELECT clip_id, platform, status, remote_id, error FROM uploads").fetchall()
    ok = {(u["clip_id"], u["platform"]) for u in ups if u["status"] in ("scheduled", "published")}

    # 1. every clip whose slot has passed must be on every enabled platform
    missing: dict[str, list[int]] = {}
    for c in clips:
        if datetime.fromisoformat(c["slot_at"]) + GRACE < now:
            for p in platforms:
                if (c["id"], p) not in ok:
                    missing.setdefault(p, []).append(c["id"])
    for p, ids in missing.items():
        last_err = next((u["error"] for u in reversed(ups) if u["platform"] == p and u["error"]), "")
        if p == "tiktok":
            msg = (f"{len(ids)} clip(s) not on TikTok yet (#{', #'.join(map(str, ids))}). "
                   "Post your TikTok drafts so new ones fit in the inbox.")
        else:
            msg = f"{len(ids)} clip(s) missing on {p}: #{', #'.join(map(str, ids))}. {last_err[:120]}"
        problems.append((f"missing-{p}", msg))

    # 2. YouTube: scheduled videos must actually go public, and none may be rejected/failed
    if "youtube" in platforms:
        yt_ids = {u["remote_id"]: u["clip_id"] for u in ups
                  if u["platform"] == "youtube" and u["status"] in ("scheduled", "published") and u["remote_id"]}
        try:
            states = _youtube_states(list(yt_ids))
            slots = {c["id"]: datetime.fromisoformat(c["slot_at"]) for c in clips}
            for vid, clip_id in yt_ids.items():
                st = states.get(vid)
                if st is None:
                    problems.append((f"yt-gone-{vid}", f"YouTube video of clip #{clip_id} ({vid}) no longer exists."))
                elif st.get("uploadStatus") in ("failed", "rejected"):
                    problems.append((f"yt-bad-{vid}", f"YouTube {st['uploadStatus']} clip #{clip_id}: "
                                                      f"{st.get('rejectionReason') or st.get('failureReason')}"))
                elif (st.get("privacyStatus") != "public" and clip_id in slots
                      and slots[clip_id] + GRACE < now):
                    problems.append((f"yt-private-{vid}", f"Clip #{clip_id} should be public on YouTube "
                                                          f"but is {st.get('privacyStatus')}."))
        except Exception as e:
            problems.append(("yt-api", f"Could not check YouTube: {str(e)[:150]}"))

    # 3. no stream may sit in one stage too long (download/transcribe/select/render)
    stuck_h = float(SETTINGS.get("autopilot", {}).get("stuck_hours", 6))
    with db.connect() as conn:
        for st in conn.execute("SELECT id, creator_id, status, created_at FROM streams WHERE status IN "
                               "('new','downloaded','analyzed')").fetchall():
            age = now - datetime.fromisoformat(st["created_at"]).replace(tzinfo=timezone.utc)
            if age > timedelta(hours=stuck_h):
                problems.append((f"stuck-{st['id']}", f"Stream #{st['id']} ({st['creator_id']}) has been "
                                 f"'{st['status']}' for {age.total_seconds() / 3600:.0f} h. Check data/logs/autopilot.log."))

    # 4. the queue must not run dry
    future = [datetime.fromisoformat(c["slot_at"]) for c in clips if datetime.fromisoformat(c["slot_at"]) > now]
    last = max(future, default=None)
    if last is None or last - now < LOW_QUEUE:
        when = last.astimezone().strftime("%a %d %b %H:%M") if last else "now"
        problems.append(("queue-low", f"Posting queue runs out {when}. Process a new stream and approve clips."))
    return problems


def notify(problems: list[tuple[str, str]]) -> None:
    """macOS notification per problem, max once every REPEAT_HOURS per problem."""
    try:
        state = json.loads(STATE.read_text())
    except (OSError, ValueError):
        state = {}
    now = time.time()
    for key, msg in problems:
        if now - state.get(key, 0) < REPEAT_HOURS * 3600:
            continue
        state[key] = now
        subprocess.run(["osascript", "-e", f"display notification {json.dumps(msg)} with title \"GTAuploads\" "
                                           f"sound name \"Glass\""], check=False)
    active = {k for k, _ in problems}
    STATE.write_text(json.dumps({k: v for k, v in state.items() if k in active}))


def report() -> int:
    problems = check()
    if not problems:
        print("✅ All good: every clip is on every enabled platform and the queue is filled.")
        return 0
    for _, msg in problems:
        print(f"⚠️  {msg}")
    return 1
