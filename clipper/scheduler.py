"""4-hour publishing slots.

On approval each clip gets the next free slot. `clipper publish-due` (run every 15 min by launchd/cron):
- YouTube: uploads early as private with publishAt=slot, so YouTube publishes on time even if this machine sleeps.
- TikTok: the API has no scheduling, so the post goes out on the first run after the slot.
"""
from __future__ import annotations

import os
import re

import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from . import db
from .config import SETTINGS, load_creators

UTC = timezone.utc


def _fast_lane() -> dict:
    """The launch fast-lane settings when today is inside it, else {}."""
    from datetime import date
    fl = SETTINGS.get("autopilot", {}).get("fast_lane") or {}
    if fl and date.fromisoformat(fl["from"]) <= date.today() <= date.fromisoformat(fl["to"]):
        return fl
    return {}


def _priority_from() -> str | None:
    """Clips from streams found after this time go before everything else (GTA 6 launch streams, owner 17 Nov)."""
    v = SETTINGS.get("autopilot", {}).get("priority_from")
    return datetime.fromisoformat(str(v)).astimezone(UTC).strftime("%Y-%m-%d %H:%M:%S") if v else None


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
    step = timedelta(hours=float(_fast_lane().get("interval_hours") or SETTINGS["schedule"]["interval_hours"]))
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
        "SELECT c.id, c.slot_at, c.score, COALESCE(s.gta6, 0) AS prio FROM clips c JOIN streams s ON s.id=c.stream_id "
        "WHERE c.status='approved' AND c.slot_at IS NOT NULL AND c.slot_at > ? "
        f"AND c.id NOT IN (SELECT clip_id FROM uploads WHERE status IN {locked})", (now,)
    ).fetchall()
    slots = sorted(r["slot_at"] for r in rows)
    ranked = sorted(rows, key=lambda r: (-r["prio"], -(r["score"] or 0), r["id"]))
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


TIKTOK_INBOX_MAX = 5  # TikTok refuses new drafts while ~5 are waiting unposted


def _tiktok_status() -> str:
    return "inbox" if SETTINGS["platforms"]["tiktok"].get("mode", "direct") == "inbox" else "published"


def _tiktok_inbox_count() -> int:
    with db.connect() as conn:
        return conn.execute("SELECT COUNT(*) FROM uploads WHERE platform='tiktok' AND status='inbox'").fetchone()[0]


def _tiktok_file(clip) -> Path:
    """The 60-90 s TikTok cut when the producer made one (Creator Rewards pays only on 1 min+), else the short."""
    long = Path(clip["path"]).with_name("tiktok.mp4")
    return long if long.exists() else Path(clip["path"])


def tiktok_watch() -> list[str]:
    """Follow every TikTok draft: posted -> 'published' (+ link); failed/expired -> 'failed' so it's re-sent once."""
    if not SETTINGS["platforms"]["tiktok"].get("enabled"):
        return []
    with db.connect() as conn:
        rows = conn.execute("SELECT clip_id, remote_id FROM uploads WHERE platform='tiktok' AND status='inbox'").fetchall()
    if not rows:
        return []
    from .uploaders import tiktok
    token = tiktok._token()
    out = []
    for r in rows:
        try:
            st = tiktok._post("/post/publish/status/fetch/", token, {"publish_id": r["remote_id"]})
        except Exception as e:
            if "not_found" in str(e) or "invalid" in str(e):
                with db.connect() as conn:
                    _record(conn, r["clip_id"], "tiktok", "failed", r["remote_id"], error=f"draft gone: {e}")
                _ledger_add(r["clip_id"], "tiktok", r["remote_id"], void=True, why="draft expired")
                out.append(f"#{r['clip_id']} TikTok draft expired; will be sent again")
            continue
        if st.get("status") == "PUBLISH_COMPLETE":
            vid = (st.get("publicaly_available_post_id") or [None])[0]
            user = SETTINGS["platforms"]["tiktok"].get("username", "")
            url = f"https://www.tiktok.com/@{user}/video/{vid}" if vid else None
            with db.connect() as conn:
                _record(conn, r["clip_id"], "tiktok", "published", r["remote_id"], url)
            out.append(f"#{r['clip_id']} posted on TikTok")
        elif st.get("status") == "FAILED":
            with db.connect() as conn:
                _record(conn, r["clip_id"], "tiktok", "failed", r["remote_id"], error=st.get("fail_reason"))
            _ledger_add(r["clip_id"], "tiktok", r["remote_id"], void=True, why="draft failed")
            out.append(f"#{r['clip_id']} TikTok draft failed ({st.get('fail_reason')}); will be sent again")
    return out


# ---------------------------------------------------------------------------------------------------------------
# Safety layer: a clip may never be posted twice, and nothing is posted while the bookkeeping is broken.
#  1. posted.jsonl ledger: written (plain file append) right after every successful post, BEFORE the database, and
#     checked before every post. Even if the database breaks, a clip is never posted a second time.
#  2. If saving a post fails, the run stops at once (SafetyStop) with a Mac alert instead of posting more.
#  3. selftest() runs before every real publish run: exercises the bookkeeping on a scratch copy of the database
#     and builds every caption. If anything fails, nothing is posted and the owner is alerted.
# ---------------------------------------------------------------------------------------------------------------
class SafetyStop(RuntimeError):
    pass


def _ledger_path() -> Path:
    from .config import DATA
    return DATA / "posted.jsonl"


def _ledger() -> dict[tuple[int, str], dict]:
    import json
    out: dict[tuple[int, str], dict] = {}
    try:
        for line in _ledger_path().read_text().splitlines():
            if line.strip():
                e = json.loads(line)
                out[(int(e["clip_id"]), e["platform"])] = e
    except FileNotFoundError:
        pass
    return {k: v for k, v in out.items() if not v.get("void")}


def _ledger_add(clip_id: int, platform: str, remote_id, void: bool = False, why: str = "") -> None:
    import json
    e = {"clip_id": int(clip_id), "platform": platform, "remote_id": remote_id,
         "at": datetime.now(UTC).isoformat(timespec="seconds")}
    if void:
        e.update(void=True, why=why)
    with open(_ledger_path(), "a") as f:
        f.write(json.dumps(e) + "\n")
        f.flush()
        os.fsync(f.fileno())


def _posted(clip_id: int, platform: str, remote_id, status: str, url=None) -> None:
    """Bookkeeping after a successful post: ledger first, then database. Any failure stops the whole run."""
    try:
        _ledger_add(clip_id, platform, remote_id)
    except Exception as e:
        _alarm(f"Could not write the posting ledger after #{clip_id} on {platform}: {e}")
        raise SafetyStop(str(e)) from e
    try:
        with db.connect() as conn:
            _record(conn, clip_id, platform, status, remote_id, url)
    except Exception as e:
        _alarm(f"#{clip_id} went live on {platform} but the database could not save it ({e}). Posting stopped.")
        raise SafetyStop(str(e)) from e


def _alarm(msg: str) -> None:
    import json
    import subprocess
    print(f"🛑 {msg}")
    subprocess.run(["osascript", "-e", f"display notification {json.dumps(msg[:220], ensure_ascii=False)} "
                                       f"with title \"GTAuploads: posting stopped\" sound name \"Basso\""], check=False)


def selftest() -> list[str]:
    """Dry checks of everything the publisher relies on. Returns a list of problems (empty = safe to post)."""
    import shutil
    import sqlite3
    import tempfile
    errors: list[str] = []
    with tempfile.TemporaryDirectory() as d:
        try:
            copy = Path(d) / "t.db"
            with db.connect() as src:
                dst = sqlite3.connect(copy)
                src.backup(dst)
                dst.close()
            conn = sqlite3.connect(copy)
            conn.row_factory = sqlite3.Row
            cid = conn.execute("SELECT id FROM clips ORDER BY id LIMIT 1").fetchone()[0]
            conn.execute("DELETE FROM uploads WHERE clip_id=? AND platform='selftest'", (cid,))
            for st in ("failed", "failed", "inbox", "scheduled", "published"):
                _record(conn, cid, "selftest", st, "x", "u", "e" if st == "failed" else None)
            r = conn.execute("SELECT status, attempts FROM uploads WHERE clip_id=? AND platform='selftest'",
                             (cid,)).fetchone()
            if (r["status"], r["attempts"]) != ("published", 2):
                errors.append(f"upload bookkeeping wrong: {tuple(r)}")
            conn.close()
        except Exception as e:
            errors.append(f"upload bookkeeping broken: {e}")
        shutil.rmtree(d, ignore_errors=True)
    try:
        creators = {c.id: c for c in load_creators()}
        with db.connect() as conn:
            rows = conn.execute("SELECT clips.*, streams.creator_id, COALESCE(streams.gta6, 0) AS prio FROM clips "
                                "JOIN streams ON streams.id=clips.stream_id WHERE clips.status IN ('approved',"
                                "'published') ORDER BY clips.id DESC LIMIT 5").fetchall()
        for c in rows:
            cr = creators.get(c["creator_id"])
            if not cr:
                continue
            for txt in (_description(c, cr), _short_caption(c, cr, "instagram"), _short_caption(c, cr, "tiktok")):
                if cr.credit not in txt:
                    errors.append(f"#{c['id']}: creator credit missing from a caption")
            if not _yt_title(c) or len(_yt_title(c)) > 100:
                errors.append(f"#{c['id']}: bad YouTube title")
    except Exception as e:
        errors.append(f"caption building broken: {e}")
    try:
        _ledger()
    except Exception as e:
        errors.append(f"posting ledger unreadable: {e}")
    return errors


NOTE_TITLE = "TikTok captions · GTAuploads"


def tiktok_captions_note() -> bool:
    """TikTok's draft API takes no caption, so every waiting draft's caption (hashtags, credit, music) goes into an
    Apple Note that syncs to the owner's iPhone: open TikTok draft, copy its caption from the note, paste, Post.
    Rewritten only when the set of waiting drafts changes. Returns True if the note was updated."""
    import hashlib
    import html
    import json
    import subprocess

    from .config import DATA
    creators = {c.id: c for c in load_creators()}
    with db.connect() as conn:
        rows = conn.execute(
            "SELECT clips.*, streams.creator_id, u.created_at AS sent FROM uploads u JOIN clips ON clips.id=u.clip_id "
            "JOIN streams ON streams.id=clips.stream_id WHERE u.platform='tiktok' AND u.status='inbox' "
            "ORDER BY u.created_at").fetchall()
    parts = [f"<h1>{NOTE_TITLE}</h1><div>Drafts waiting: {len(rows)}. Oldest first (same order as your TikTok "
             "drafts, bottom to top). Copy the text under each title into its draft, then Post.</div><div><br></div>"]
    for r in rows:
        cr = creators.get(r["creator_id"])
        cap = _short_caption(r, cr, "tiktok")
        parts.append(f"<h2>#{r['id']} · {html.escape(r['title'][:60])}</h2>"
                     + "".join(f"<div>{html.escape(line) or '<br>'}</div>" for line in cap.split("\n"))
                     + "<div><br></div>")
    if not rows:
        parts.append("<div>No drafts waiting 🎉</div>")
    body = "".join(parts)
    state = DATA / "tiktok_note.hash"
    digest = hashlib.sha1(body.encode()).hexdigest()
    if state.exists() and state.read_text() == digest:
        return False
    script = f"""
tell application "Notes"
    set b to {json.dumps(body, ensure_ascii=False)}
    set found to (notes whose name is {json.dumps(NOTE_TITLE, ensure_ascii=False)})
    if (count of found) > 0 then
        set body of item 1 of found to b
    else
        make new note with properties {{body:b}}
    end if
end tell"""
    r = subprocess.run(["osascript", "-e", script], capture_output=True, text=True, timeout=60)
    if r.returncode:
        raise RuntimeError(r.stderr.strip()[:200])
    state.write_text(digest)
    return True


def _record(conn, clip_id, platform, status, remote_id=None, url=None, error=None):
    conn.execute(
        "INSERT INTO uploads (clip_id, platform, remote_id, url, status, error, attempts) "
        "VALUES (?,?,?,?,?,?, CASE WHEN ?='failed' THEN 1 ELSE 0 END) "
        "ON CONFLICT(clip_id, platform) DO UPDATE SET remote_id=excluded.remote_id, url=excluded.url, "
        "status=excluded.status, error=excluded.error, "
        "attempts=CASE WHEN excluded.status='failed' THEN COALESCE(uploads.attempts,0)+1 ELSE uploads.attempts END",
        (clip_id, platform, remote_id, url, status, error, status),
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


HASHTAG_RE = re.compile(r"(?<!\w)#\w+")


def _is_gta6(clip) -> bool:
    """GTA 6 gameplay stream, or a clip that is about GTA 6 (launch, download, leaks) even if played in GTA 5."""
    from .download import GTA6_RE
    if "prio" in clip.keys() and clip["prio"]:
        return True
    pk = _pack(clip)
    text = " ".join(str(x or "") for x in (clip["title"], clip["caption"], pk.get("youtube_title")))
    if GTA6_RE.search(text):
        return True
    with db.connect() as conn:
        r = conn.execute("SELECT COALESCE(gta6,0) FROM streams WHERE id=?", (clip["stream_id"],)).fetchone()
    return bool(r and r[0])


def _yt_hashtags(clip, creator=None) -> list[str]:
    """3-5 hashtags, most important first (YouTube shows the first 3 above the title):
    game, creator, Spanish GTA community, then the growth agent's specific ones. Never #Shorts (no effect) and
    never GTA 6 hashtags on a clip that has nothing to do with GTA 6 (misleading tags hurt reach)."""
    from .download import GTA6_RE
    g6 = _is_gta6(clip)
    base = ["#GTA6"] if g6 else ["#GTA5", "#GTA"]
    if creator:
        base.append("#" + re.sub(r"\W", "", creator.display_name))
    base.append("#GTA6Español" if g6 else "#GTAenEspañol")
    extra = [t if t.startswith("#") else f"#{t}" for t in (_pack(clip).get("youtube_hashtags") or [])]
    extra = [t.replace(" ", "") for t in extra if t.lower() != "#shorts" and (g6 or not GTA6_RE.search(t))]
    out, seen = [], set()
    for t in base + extra:
        if t.lower() not in seen:
            seen.add(t.lower())
            out.append(t)
    return out[:5]


def _yt_title(clip) -> str:
    """Growth agent's title + the game hashtag (a clickable link in a Short's title) when it fits."""
    t = (_pack(clip).get("youtube_title") or clip["title"]).strip()
    if "#" not in t:
        tag = " #GTA6" if _is_gta6(clip) else " #GTA"
        if len(t) + len(tag) <= 100:
            t += tag
    return t[:100]


def _yt_tags(clip) -> list[str]:
    """Search tags: game-correct base tags + the growth agent's (GTA 6 ones dropped on non-GTA 6 clips)."""
    from .download import GTA6_RE
    g6 = _is_gta6(clip)
    base = (["GTA 6", "GTA6", "GTA VI", "Grand Theft Auto VI", "GTA 6 español"] if g6
            else ["GTA", "GTA 5", "GTA V", "Grand Theft Auto V", "GTA en español"])
    own = [t.lstrip("#") for t in (_pack(clip).get("youtube_tags") or []) if t.lower().lstrip("#") != "shorts"]
    own = [t for t in own if g6 or not (GTA6_RE.search(t) or "grand theft auto vi" in t.lower())]
    return list(dict.fromkeys(base + own))


FOLLOW_RE = re.compile(r"s[ií]gue(nos|me)|suscr[ií]bete|follow", re.I)


def _with_cta(body: str, platform: str) -> str:
    """Owner 18 Nov: every post asks viewers to follow (added by code so it's never forgotten, once only)."""
    cta = (SETTINGS.get("follow_cta") or {}).get(platform)
    if not cta or FOLLOW_RE.search(body or ""):
        return body
    return f"{body.rstrip()}\n\n{cta}" if body else cta


def _description(clip, creator) -> str:
    pk = _pack(clip)
    body = HASHTAG_RE.sub("", pk.get("youtube_description") or clip["caption"] or "")  # hashtags go in one place
    body = re.sub(r"[ \t]+\n|[ \t]{2,}", lambda m: "\n" if "\n" in m.group() else " ", body).strip()
    parts = [_with_cta(body, "youtube"), ""]
    parts.append(f"Clip from {creator.credit}.")  # credit lines are always added by code, never left to an agent
    if creator.links.get("description"):
        parts.append(creator.links["description"])
    if mc := _music_credit(clip):
        parts.append(mc)
    parts += ["", " ".join(_yt_hashtags(clip, creator))]
    return "\n".join(parts).strip()


def _short_caption(clip, creator=None, platform: str = "tiktok") -> str:
    """TikTok/Instagram caption: growth agent's caption + hashtags (or title + hashtags), creator + music credit."""
    pk = _pack(clip)
    body = pk.get(f"{platform}_caption")
    tags = _tags(pk.get(f"{platform}_hashtags")) if body else ""
    if not body:
        body, tags = clip["title"], clip["hashtags"] or ""
    text = f"{_with_cta(body, platform)}\n\n{tags}".strip()
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
    if (clip_id, platform) in _ledger():
        print(f"⛔ #{clip_id} is already on {platform} (posting ledger). Not posting twice.")
        return 2
    print(f"#{clip_id} -> {platform} now")
    try:
        if platform == "tiktok":
            from .uploaders import tiktok
            rid = tiktok.upload(_tiktok_file(clip), _short_caption(clip, creator, "tiktok"))
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
    _posted(clip_id, platform, rid, _tiktok_status() if platform == "tiktok" else "published", url)
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
            problems = selftest()
            if problems:
                _alarm("Self-test failed, nothing posted: " + "; ".join(problems))
                return
        if not dry_run:
            try:
                for m in tiktok_watch():
                    print(m)
            except Exception as e:
                print(f"TikTok draft check failed: {e}")
            try:
                for m in sync_youtube_schedule():
                    print(m)
            except Exception as e:
                print(f"YouTube schedule sync failed: {e}")
        try:
            _publish_due(dry_run)
        except SafetyStop:
            return  # already alerted; nothing more is posted until the problem is fixed
        if not dry_run and SETTINGS["platforms"]["tiktok"].get("mode") == "inbox":
            try:
                if tiktok_captions_note():
                    print(f"TikTok captions updated in Notes ('{NOTE_TITLE}')")
            except Exception as e:  # never let the note break publishing
                print(f"TikTok captions note failed: {e}")
        if not dry_run:
            try:
                from . import health
                health.notify(health.check())
            except Exception as e:  # the health check must never break publishing
                print(f"health check failed: {e}")


def _upload_ahead_hours() -> float:
    """During the launch fast lane, upload only a short way ahead so YouTube's daily upload cap stays free for
    fresh launch clips (the clips already uploaded keep publishing even if the Mac is off)."""
    from datetime import date
    fl = SETTINGS.get("autopilot", {}).get("fast_lane") or {}
    if fl.get("youtube_upload_ahead_hours") and \
            date.fromisoformat(fl["from"]) <= date.today() <= date.fromisoformat(fl["to"]):
        return float(fl["youtube_upload_ahead_hours"])
    return float(SETTINGS["schedule"]["youtube_upload_ahead_hours"])


def _publish_due(dry_run: bool = False) -> None:
    now = datetime.now(UTC)
    plat = SETTINGS["platforms"]
    ahead = timedelta(hours=_upload_ahead_hours())
    creators = {c.id: c for c in load_creators()}

    with db.connect() as conn:
        reprioritize(conn)
        clips = conn.execute(
            "SELECT clips.*, streams.creator_id, COALESCE(streams.gta6, 0) AS prio FROM clips "
            "JOIN streams ON streams.id = clips.stream_id "
            "WHERE clips.status IN ('approved','published') AND slot_at IS NOT NULL ORDER BY slot_at").fetchall()
        clips = sorted(clips, key=lambda c: -c["prio"])  # launch clips get YouTube's upload cap first
        yt_24h = conn.execute("SELECT COUNT(*) FROM uploads WHERE platform='youtube' AND remote_id IS NOT NULL "
                              "AND created_at > datetime('now','-1 day')").fetchone()[0]
        done = {(r["clip_id"], r["platform"]) for r in conn.execute(
            "SELECT clip_id, platform FROM uploads WHERE status IN ('scheduled','published','inbox') "
            "OR (status='failed' AND attempts >= ?)", (MAX_ATTEMPTS,))}
    done |= set(_ledger())  # posted per the ledger = posted, whatever the database says

    fl = _fast_lane()
    yt_cap = int(SETTINGS["platforms"]["youtube"].get("daily_upload_cap", 10))
    yt_reserve = int(fl.get("youtube_reserve_for_priority", 0))

    tiktok_full = yt_full = False
    with db.connect() as conn:  # catch-up posts (clip's slot long past): at most one per platform every 3 h
        recent = {r["platform"] for r in conn.execute(
            "SELECT DISTINCT platform FROM uploads WHERE created_at > datetime('now','-3 hours') "
            "AND status IN ('scheduled','published','inbox')")}
    for clip in clips:
        slot = _parse(clip["slot_at"])
        late = slot < now - timedelta(hours=2) and not fl  # fast lane: catch up at full speed
        creator = creators.get(clip["creator_id"])
        if not creator or not creator.permitted:
            print(f"#{clip['id']}: creator '{clip['creator_id']}' no longer permitted. Skipping.")
            continue
        if not clip["path"] or not Path(clip["path"]).exists():
            print(f"#{clip['id']}: video file missing. Skipping.")
            continue
        desc = _description(clip, creator)

        yt_reserved = not clip["prio"] and yt_24h >= yt_cap - yt_reserve  # keep uploads free for launch clips
        if (plat["youtube"]["enabled"] and not yt_full and not yt_reserved and (clip["id"], "youtube") not in done
                and slot - now <= ahead):
            publish_at = slot if slot > now + timedelta(minutes=15) else None
            print(f"#{clip['id']} -> YouTube ({'scheduled ' + fmt_local(clip['slot_at']) if publish_at else 'now'})")
            if not dry_run:
                from .uploaders import youtube
                try:
                    vid = youtube.upload(Path(clip["path"]), _yt_title(clip), desc, publish_at, _yt_tags(clip))
                    _posted(clip["id"], "youtube", vid, "scheduled" if publish_at else "published",
                            f"https://youtube.com/shorts/{vid}")
                    yt_24h += 1
                except SafetyStop:
                    raise
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
              and _tiktok_inbox_count() < TIKTOK_INBOX_MAX
              and not (late and "tiktok" in recent)):
            print(f"#{clip['id']} -> TikTok")
            if not dry_run:
                from .uploaders import tiktok
                try:
                    pid = tiktok.upload(_tiktok_file(clip), _short_caption(clip, creator, "tiktok"))
                    _posted(clip["id"], "tiktok", pid, _tiktok_status())
                    recent.add("tiktok")
                except SafetyStop:
                    raise
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
                    _posted(clip["id"], "instagram", mid, "published", instagram.permalink(mid))
                    recent.add("instagram")
                except SafetyStop:
                    raise
                except Exception as e:
                    print(f"   Instagram failed: {e}")
                    with db.connect() as conn:
                        _record(conn, clip["id"], "instagram", "failed", error=str(e))

        if slot <= now and not dry_run:
            with db.connect() as conn:
                ok = {r["platform"] for r in conn.execute(
                    "SELECT platform FROM uploads WHERE clip_id=? AND status IN ('scheduled','published','inbox')",
                    (clip["id"],))}
                need = {p for p in ("youtube", "tiktok", "instagram") if plat.get(p, {}).get("enabled")}
                if need <= ok:
                    conn.execute("UPDATE clips SET status='published' WHERE id=?", (clip["id"],))
