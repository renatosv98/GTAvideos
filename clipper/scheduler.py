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
    # Clips only *scheduled* on YouTube can still move if we have YouTube edit permission (sync_youtube_schedule
    # then moves their publish time); anything already live somewhere keeps its slot.
    from .uploaders import youtube
    locked = "('published')" if youtube.can_edit() else "('scheduled','published')"
    now = datetime.now(UTC).isoformat()
    rows = conn.execute(
        "SELECT id, slot_at, score FROM clips WHERE status='approved' AND slot_at IS NOT NULL AND slot_at > ? "
        f"AND id NOT IN (SELECT clip_id FROM uploads WHERE status IN {locked})", (now,)
    ).fetchall()
    slots = sorted(r["slot_at"] for r in rows)
    ranked = sorted(rows, key=lambda r: (-(r["score"] or 0), r["id"]))
    for r, slot in zip(ranked, slots):
        conn.execute("UPDATE clips SET slot_at=? WHERE id=?", (slot, r["id"]))


MAX_ATTEMPTS = 5  # after this many failures the publisher stops; the manager decides (retry or escalate)


def sync_youtube_schedule() -> list[str]:
    """Make YouTube's publish time match our slot for clips that are scheduled there (after re-prioritising)."""
    from .uploaders import youtube
    if not youtube.can_edit():
        return []
    with db.connect() as conn:
        rows = conn.execute("SELECT u.clip_id, u.remote_id, c.slot_at FROM uploads u JOIN clips c ON c.id=u.clip_id "
                            "WHERE u.platform='youtube' AND u.status='scheduled' AND c.status='approved'").fetchall()
    if not rows:
        return []
    from googleapiclient.discovery import build
    yt = build("youtube", "v3", credentials=youtube._creds(), cache_discovery=False)
    live = {v["id"]: v["status"] for v in yt.videos().list(
        part="status", id=",".join(r["remote_id"] for r in rows[:50])).execute().get("items", [])}
    out = []
    for r in rows:
        st = live.get(r["remote_id"], {})
        want = _parse(r["slot_at"])
        have = st.get("publishAt")
        if st.get("privacyStatus") == "private" and have and abs((_parse(have.replace("Z", "+00:00")) - want)
                                                               .total_seconds()) > 60 and want > datetime.now(UTC):
            youtube.update(r["remote_id"], publish_at=want)
            out.append(f"#{r['clip_id']} YouTube moved to {fmt_local(r['slot_at'])}")
    return out


def _record(conn, clip_id, platform, status, remote_id=None, url=None, error=None):
    conn.execute(
        "INSERT INTO uploads (clip_id, platform, remote_id, url, status, error) VALUES (?,?,?,?,?,?) "
        "ON CONFLICT(clip_id, platform) DO UPDATE SET remote_id=excluded.remote_id, url=excluded.url, "
        "status=excluded.status, error=excluded.error, "
        "attempts=CASE WHEN excluded.status='failed' THEN COALESCE(uploads.attempts,0)+1 ELSE uploads.attempts END",
        (clip_id, platform, remote_id, url, status, error),
    )


def _music_credit(clip) -> str | None:
    import json

    from . import music
    title = json.loads(clip["edit_json"] or "{}").get("music") if "edit_json" in clip.keys() else None
    track = next((t for t in music.tracks() if t["title"] == title), None) if title else None
    return music.credit(track) if track else None


def _pack(clip) -> dict:
    """Per-platform title/caption/hashtags written by the growth agent before publishing (empty if none)."""
    import json
    return (json.loads(clip["edit_json"] or "{}").get("packaging") or {}) if "edit_json" in clip.keys() else {}


def _tags(tags) -> str:
    return " ".join(t if t.startswith("#") else f"#{t}" for t in (tags or []))


def _yt_title(clip) -> str:
    return (_pack(clip).get("youtube_title") or clip["title"])[:100]


def _yt_tags(clip) -> list[str]:
    return _pack(clip).get("youtube_tags") or []


def _description(clip, creator) -> str:
    pk = _pack(clip)
    parts = [pk.get("youtube_description") or clip["caption"] or "", ""]
    parts.append(f"Clip from {creator.credit}.")  # credit lines are always added by code, never left to an agent
    if creator.links.get("description"):
        parts.append(creator.links["description"])
    if mc := _music_credit(clip):
        parts.append(mc)
    parts += ["", _tags(pk.get("youtube_hashtags")) or clip["hashtags"] or ""]
    return "\n".join(parts).strip()


def _short_caption(clip, creator=None, platform: str = "tiktok") -> str:
    """TikTok/Instagram caption: growth agent's caption + hashtags (or title + hashtags), creator + music credit."""
    pk = _pack(clip)
    body = pk.get(f"{platform}_caption")
    tags = _tags(pk.get(f"{platform}_hashtags")) if body else ""
    text = f"{body}\n\n{tags}".strip() if body else f"{clip['title']} {clip['hashtags'] or ''}".strip()
    if creator:
        text += f"\n🎥 {creator.credit}"
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
            rid = tiktok.upload(Path(clip["path"]), _short_caption(clip, creator, "tiktok"))
            url = None
        elif platform == "instagram":
            from .uploaders import instagram
            rid = instagram.upload(Path(clip["path"]), _short_caption(clip, creator, "instagram"))
            url = instagram.permalink(rid)
        else:
            from .uploaders import youtube
            rid = youtube.upload(Path(clip["path"]), _yt_title(clip), _description(clip, creator), None, _yt_tags(clip))
            url = f"https://youtube.com/shorts/{rid}"
    except Exception as e:
        print(f"❌ {e}")
        return 1
    with db.connect() as conn:
        _record(conn, clip_id, platform, "published", rid, url)
    print(f"✅ Sent ({rid}). " + ("Check your TikTok inbox/drafts." if platform == "tiktok" else url or ""))
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
        if not dry_run:
            try:
                for m in sync_youtube_schedule():
                    print(m)
            except Exception as e:
                print(f"YouTube schedule sync failed: {e}")
        _publish_due(dry_run)
        if not dry_run:
            try:
                from . import health
                health.notify(health.check())
            except Exception as e:  # the health check must never break publishing
                print(f"health check failed: {e}")


def _publish_due(dry_run: bool = False) -> None:
    now = datetime.now(UTC)
    plat = SETTINGS["platforms"]
    ahead = timedelta(hours=SETTINGS["schedule"]["youtube_upload_ahead_hours"])
    creators = {c.id: c for c in load_creators()}

    with db.connect() as conn:
        reprioritize(conn)
        clips = conn.execute(
            "SELECT clips.*, streams.creator_id FROM clips JOIN streams ON streams.id = clips.stream_id "
            "WHERE clips.status IN ('approved','published') AND slot_at IS NOT NULL ORDER BY slot_at"
        ).fetchall()
        done = {(r["clip_id"], r["platform"]) for r in conn.execute(
            "SELECT clip_id, platform FROM uploads WHERE status IN ('scheduled','published') "
            "OR (status='failed' AND attempts >= ?)", (MAX_ATTEMPTS,))}

    tiktok_full = yt_full = False
    with db.connect() as conn:  # catch-up posts (clip's slot long past): at most one per platform every 3 h
        recent = {r["platform"] for r in conn.execute(
            "SELECT DISTINCT platform FROM uploads WHERE created_at > datetime('now','-3 hours') "
            "AND status IN ('scheduled','published')")}
    for clip in clips:
        slot = _parse(clip["slot_at"])
        late = slot < now - timedelta(hours=2)
        creator = creators.get(clip["creator_id"])
        if not creator or not creator.permitted:
            print(f"#{clip['id']}: creator '{clip['creator_id']}' no longer permitted. Skipping.")
            continue
        if not clip["path"] or not Path(clip["path"]).exists():
            print(f"#{clip['id']}: video file missing. Skipping.")
            continue
        desc = _description(clip, creator)

        if plat["youtube"]["enabled"] and not yt_full and (clip["id"], "youtube") not in done and slot - now <= ahead:
            publish_at = slot if slot > now + timedelta(minutes=15) else None
            print(f"#{clip['id']} -> YouTube ({'scheduled ' + fmt_local(clip['slot_at']) if publish_at else 'now'})")
            if not dry_run:
                from .uploaders import youtube
                try:
                    vid = youtube.upload(Path(clip["path"]), _yt_title(clip), desc, publish_at, _yt_tags(clip))
                    with db.connect() as conn:
                        _record(conn, clip["id"], "youtube", "scheduled" if publish_at else "published",
                                vid, f"https://youtube.com/shorts/{vid}")
                except Exception as e:
                    if "uploadLimitExceeded" in str(e):
                        # new channels have a daily upload cap; the clip stays queued and is retried next run
                        print("   YouTube daily upload limit reached; will retry later")
                        yt_full = True
                    else:
                        print(f"   YouTube failed: {e}")
                        with db.connect() as conn:
                            _record(conn, clip["id"], "youtube", "failed", error=str(e))

        if tiktok_full:
            pass
        elif (plat["tiktok"]["enabled"] and (clip["id"], "tiktok") not in done and slot <= now
              and not (late and "tiktok" in recent)):
            print(f"#{clip['id']} -> TikTok")
            if not dry_run:
                from .uploaders import tiktok
                try:
                    pid = tiktok.upload(Path(clip["path"]), _short_caption(clip, creator, "tiktok"))
                    with db.connect() as conn:
                        _record(conn, clip["id"], "tiktok", "published", pid)
                    recent.add("tiktok")
                except Exception as e:
                    if "too_many_pending" in str(e):
                        # TikTok allows only ~5 unposted drafts; wait until the owner posts some, retry next run
                        print("   TikTok inbox full (post your drafts in the app); will retry")
                        tiktok_full = True
                    else:
                        print(f"   TikTok failed: {e}")
                        with db.connect() as conn:
                            _record(conn, clip["id"], "tiktok", "failed", error=str(e))

        if (plat.get("instagram", {}).get("enabled") and (clip["id"], "instagram") not in done and slot <= now
                and not (late and "instagram" in recent)):
            print(f"#{clip['id']} -> Instagram")
            if not dry_run:
                from .uploaders import instagram
                try:
                    mid = instagram.upload(Path(clip["path"]), _short_caption(clip, creator, "instagram"))
                    with db.connect() as conn:
                        _record(conn, clip["id"], "instagram", "published", mid, instagram.permalink(mid))
                    recent.add("instagram")
                except Exception as e:
                    print(f"   Instagram failed: {e}")
                    with db.connect() as conn:
                        _record(conn, clip["id"], "instagram", "failed", error=str(e))

        if slot <= now and not dry_run:
            with db.connect() as conn:
                ok = {r["platform"] for r in conn.execute(
                    "SELECT platform FROM uploads WHERE clip_id=? AND status IN ('scheduled','published')", (clip["id"],))}
                need = {p for p in ("youtube", "tiktok", "instagram") if plat.get(p, {}).get("enabled")}
                if need <= ok:
                    conn.execute("UPDATE clips SET status='published' WHERE id=?", (clip["id"],))
