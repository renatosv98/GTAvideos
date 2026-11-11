"""4-hour publishing slots.

On approval each clip gets the next free slot. `clipper publish-due` (run every 15 min by launchd/cron):
- YouTube: uploads early as private with publishAt=slot, so YouTube publishes on time even if this machine sleeps.
- TikTok: the API has no scheduling, so the post goes out on the first run after the slot.
"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from . import db
from .config import SETTINGS, load_creators

UTC = timezone.utc


def _tz() -> ZoneInfo:
    return ZoneInfo(SETTINGS["schedule"]["timezone"])


def _parse(s: str) -> datetime:
    return datetime.fromisoformat(s).astimezone(UTC)


def fmt_local(s: str) -> str:
    return _parse(s).astimezone(_tz()).strftime("%a %d %b %H:%M")


def next_slot_after(t: datetime) -> datetime:
    cfg = SETTINGS["schedule"]
    step = int(cfg["interval_hours"])
    local = t.astimezone(_tz())
    day0 = local.replace(hour=int(cfg["anchor_hour"]) % 24, minute=0, second=0, microsecond=0)
    if day0 > local:
        day0 -= timedelta(days=1)
    slot = day0
    while slot <= local:
        slot += timedelta(hours=step)
    return slot.astimezone(UTC)


def assign_slot(conn: sqlite3.Connection) -> str:
    step = timedelta(hours=int(SETTINGS["schedule"]["interval_hours"]))
    candidate = next_slot_after(datetime.now(UTC) + timedelta(minutes=10))
    row = conn.execute(
        "SELECT MAX(slot_at) AS m FROM clips WHERE status IN ('approved','published') AND slot_at IS NOT NULL"
    ).fetchone()
    if row["m"]:
        candidate = max(candidate, _parse(row["m"]) + step)
    return candidate.isoformat()


def reprioritize(conn: sqlite3.Connection) -> None:
    """Re-deal the open slots so the highest viral-chance clip always goes out next.
    Clips already uploaded somewhere (e.g. scheduled on YouTube) keep their slot."""
    rows = conn.execute(
        "SELECT id, slot_at, score FROM clips WHERE status='approved' AND slot_at IS NOT NULL "
        "AND id NOT IN (SELECT clip_id FROM uploads WHERE status IN ('scheduled','published'))"
    ).fetchall()
    slots = sorted(r["slot_at"] for r in rows)
    ranked = sorted(rows, key=lambda r: (-(r["score"] or 0), r["id"]))
    for r, slot in zip(ranked, slots):
        conn.execute("UPDATE clips SET slot_at=? WHERE id=?", (slot, r["id"]))


def _record(conn, clip_id, platform, status, remote_id=None, url=None, error=None):
    conn.execute(
        "INSERT INTO uploads (clip_id, platform, remote_id, url, status, error) VALUES (?,?,?,?,?,?) "
        "ON CONFLICT(clip_id, platform) DO UPDATE SET remote_id=excluded.remote_id, url=excluded.url, "
        "status=excluded.status, error=excluded.error",
        (clip_id, platform, remote_id, url, status, error),
    )


def _music_credit(clip) -> str | None:
    import json

    from . import music
    title = json.loads(clip["edit_json"] or "{}").get("music") if "edit_json" in clip.keys() else None
    track = next((t for t in music.tracks() if t["title"] == title), None) if title else None
    return music.credit(track) if track else None


def _description(clip, creator) -> str:
    parts = [clip["caption"] or "", ""]
    parts.append(f"Clip from {creator.credit}.")
    if creator.links.get("description"):
        parts.append(creator.links["description"])
    if mc := _music_credit(clip):
        parts.append(mc)
    parts += ["", clip["hashtags"] or ""]
    return "\n".join(parts).strip()


def _short_caption(clip) -> str:
    """TikTok/Instagram caption: title + hashtags + music credit."""
    text = f"{clip['title']} {clip['hashtags'] or ''}".strip()
    return f"{text}\n{mc}" if (mc := _music_credit(clip)) else text


def publish_now(clip_id: int, platform: str) -> int:
    creators = {c.id: c for c in load_creators()}
    with db.connect() as conn:
        clip = conn.execute(
            "SELECT clips.*, streams.creator_id FROM clips JOIN streams ON streams.id = clips.stream_id WHERE clips.id=?",
            (clip_id,)).fetchone()
    if not clip or clip["status"] not in ("approved", "published"):
        print(f"⛔ Clip #{clip_id} must be approved in `clipper review` first.")
        return 2
    creator = creators.get(clip["creator_id"])
    if not creator or not creator.permitted:
        print(f"⛔ Creator '{clip['creator_id']}' is not permitted.")
        return 2
    print(f"#{clip_id} -> {platform} now")
    try:
        if platform == "tiktok":
            from .uploaders import tiktok
            rid = tiktok.upload(Path(clip["path"]), _short_caption(clip))
            url = None
        else:
            from .uploaders import youtube
            rid = youtube.upload(Path(clip["path"]), clip["title"], _description(clip, creator), None)
            url = f"https://youtube.com/shorts/{rid}"
    except Exception as e:
        print(f"❌ {e}")
        return 1
    with db.connect() as conn:
        _record(conn, clip_id, platform, "published", rid, url)
    print(f"✅ Sent ({rid}). " + ("Check your TikTok inbox/drafts." if platform == "tiktok" else url))
    return 0


def publish_due(dry_run: bool = False) -> None:
    """Only one publisher at a time: two overlapping runs (e.g. launchd + a manual run) would upload twice."""
    import fcntl

    from .config import DATA
    DATA.mkdir(parents=True, exist_ok=True)
    with open(DATA / "publish.lock", "w") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            print("Another publish run is in progress; skipping this one.")
            return
        _publish_due(dry_run)


def _publish_due(dry_run: bool = False) -> None:
    now = datetime.now(UTC)
    plat = SETTINGS["platforms"]
    ahead = timedelta(hours=SETTINGS["schedule"]["youtube_upload_ahead_hours"])
    creators = {c.id: c for c in load_creators()}

    with db.connect() as conn:
        reprioritize(conn)
        clips = conn.execute(
            "SELECT clips.*, streams.creator_id FROM clips JOIN streams ON streams.id = clips.stream_id "
            "WHERE clips.status = 'approved' AND slot_at IS NOT NULL ORDER BY slot_at"
        ).fetchall()
        done = {(r["clip_id"], r["platform"]) for r in conn.execute(
            "SELECT clip_id, platform FROM uploads WHERE status IN ('scheduled','published')")}

    for clip in clips:
        slot = _parse(clip["slot_at"])
        creator = creators.get(clip["creator_id"])
        if not creator or not creator.permitted:
            print(f"#{clip['id']}: creator '{clip['creator_id']}' no longer permitted. Skipping.")
            continue
        if not clip["path"] or not Path(clip["path"]).exists():
            print(f"#{clip['id']}: video file missing. Skipping.")
            continue
        desc = _description(clip, creator)

        if plat["youtube"]["enabled"] and (clip["id"], "youtube") not in done and slot - now <= ahead:
            publish_at = slot if slot > now + timedelta(minutes=15) else None
            print(f"#{clip['id']} -> YouTube ({'scheduled ' + fmt_local(clip['slot_at']) if publish_at else 'now'})")
            if not dry_run:
                from .uploaders import youtube
                try:
                    vid = youtube.upload(Path(clip["path"]), clip["title"], desc, publish_at)
                    with db.connect() as conn:
                        _record(conn, clip["id"], "youtube", "scheduled" if publish_at else "published",
                                vid, f"https://youtube.com/shorts/{vid}")
                except Exception as e:
                    print(f"   YouTube failed: {e}")
                    with db.connect() as conn:
                        _record(conn, clip["id"], "youtube", "failed", error=str(e))

        if plat["tiktok"]["enabled"] and (clip["id"], "tiktok") not in done and slot <= now:
            print(f"#{clip['id']} -> TikTok")
            if not dry_run:
                from .uploaders import tiktok
                try:
                    pid = tiktok.upload(Path(clip["path"]), _short_caption(clip))
                    with db.connect() as conn:
                        _record(conn, clip["id"], "tiktok", "published", pid)
                except Exception as e:
                    print(f"   TikTok failed: {e}")
                    with db.connect() as conn:
                        _record(conn, clip["id"], "tiktok", "failed", error=str(e))

        if slot <= now and not dry_run:
            with db.connect() as conn:
                ok = {r["platform"] for r in conn.execute(
                    "SELECT platform FROM uploads WHERE clip_id=? AND status IN ('scheduled','published')", (clip["id"],))}
                need = {p for p in ("youtube", "tiktok") if plat[p]["enabled"]}
                if need <= ok:
                    conn.execute("UPDATE clips SET status='published' WHERE id=?", (clip["id"],))
