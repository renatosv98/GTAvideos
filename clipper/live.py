"""Live mode: clip a GTA 6 stream WHILE it is still live.

Every 10 min (launchd com.gta6clipper.live) for each creator with `live_chunks: true` in creators.yaml:
if they are live (or the stream we started on just ended), take the newest recording (Twitch keeps the VOD of the
stream in progress), and as soon as `first_chunk_min` (then `chunk_min`) more minutes of GTA are recorded, process
that chunk like a normal stream: download only that part, transcribe, pick the best moments, render, auto-approve.
GTA 6 clips jump to the front of the posting queue. When the stream ends, the remainder is processed and the VOD
is marked seen so the normal scout never processes it again.

While a live stream is being clipped, the hourly autopilot does not start other downloads (data/live_active),
so the Mac's power goes to the live clips.
"""
from __future__ import annotations

import fcntl
import json
import subprocess
import time
import traceback

from . import discover
from .config import DATA, _load, load_creators
from .download import GTA6_RE, part_url, probe

STATE = DATA / "live_parts.json"
ACTIVE = DATA / "live_active"
SAFETY = 120  # seconds left out at the live edge (still being written)


def _state() -> dict:
    try:
        return json.loads(STATE.read_text())
    except (OSError, ValueError):
        return {}


def _save(st: dict) -> None:
    STATE.write_text(json.dumps(st, indent=1))


def _notify(msg: str) -> None:
    subprocess.run(["osascript", "-e", f"display notification {json.dumps(msg, ensure_ascii=False)} "
                                       f"with title \"GTAuploads live\" sound name \"Glass\""], check=False)


def gta_spans(info: dict, now_dur: float) -> list[tuple[float, float]]:
    """GTA 6 chapters as (start, end); the chapter in progress (end == start on a live VOD) runs to now.
    GTA 6 only: marathon streams switch between GTA VI, GTA V and Just Chatting, and those must never be
    clipped as GTA 6."""
    out = []
    for c in info.get("chapters") or []:
        if GTA6_RE.search(c.get("title") or ""):
            end = c["end_time"] if c["end_time"] > c["start_time"] else now_dur
            out.append((float(c["start_time"]), float(end)))
    return sorted(out)


def next_chunk(spans, done_to: float, edge: float, n_done: int, live: bool, cfg: dict) -> tuple[float, float] | None:
    """Next part to process, always inside ONE GTA 6 chapter (never across other games / Just Chatting).
    The chapter still being recorded (touching the live edge) waits for a full chunk; a finished chapter is taken
    whole (in max_chunk_min pieces); leftovers under 2 min are skipped. Once the stream has ended, any remainder
    of 5+ min is taken."""
    cap = 60 * float(cfg.get("max_chunk_min", 90))
    for s, e in sorted(spans):
        a, b = max(done_to, s), min(e, edge)
        if b - a < 120:
            if e >= edge:  # nothing more recorded yet
                return None
            continue  # tiny or already-done chapter: skip it
        open_ = live and e >= edge  # this chapter is still being recorded
        if open_:
            need = 60 * float(cfg.get("first_chunk_min" if n_done == 0 else "chunk_min", 45 if n_done == 0 else 60))
            if b - a < need:
                return None
        elif not live and e >= edge and b - a < 300:
            return None
        return (a, min(b, a + cap))  # fell behind: keep chunks quick to turn around
    return None


def _watchdog() -> None:
    """The publisher must run every 15 min; if its heartbeat is older than 25 min, start it (launchd timers can
    silently stop firing, as the live job's did on 18 Nov)."""
    from datetime import datetime, timezone
    try:
        last = datetime.fromisoformat((DATA / "publish_last.txt").read_text().strip())
        stale = (datetime.now(timezone.utc) - last).total_seconds() > 25 * 60
    except (OSError, ValueError):
        stale = True
    if stale:
        print("watchdog: publisher hasn't run for 25+ min, starting it")
        _publish_soon()


def watch() -> int:
    DATA.mkdir(parents=True, exist_ok=True)
    _watchdog()
    with open(DATA / "live.lock", "w") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            print("live watcher already running")
            return 0
        raw = {c["id"]: c for c in _load("creators.yaml").get("creators", [])}
        st = _state()
        busy = False
        progress = True
        while progress:  # take turns: one chunk per live creator per round, so nobody waits behind a long catch-up
            progress = False
            for creator in load_creators():
                if _one_chunk(creator, raw, st):
                    progress = True
                busy = busy or bool(_BUSY)
        if not busy:
            ACTIVE.unlink(missing_ok=True)
    return 0


_BUSY: list = []


def _one_chunk(creator, raw: dict, st: dict) -> bool:
    """Process at most one new chunk of this creator's live GTA stream. True if a chunk was processed."""
    from .pipeline import process
    cfg = raw.get(creator.id, {})
    if not (creator.permitted and cfg.get("live_chunks") and cfg.get("vods") and cfg.get("live")):
        return False
    live = discover._is_live(cfg["live"])
    try:
        newest = discover._recent(cfg["vods"])[:1]
    except Exception as e:
        print(f"{creator.display_name}: could not list recordings: {e}")
        return False
    if not newest:
        return False
    vod = discover._clean(newest[0].get("url") or "")
    rec = st.get(vod)
    if not live and (not rec or rec.get("ended")):
        return False  # not live, and not a stream we were clipping live
    info = probe(vod)
    still = bool(info.get("is_live"))
    dur = float(info.get("duration") or 0)
    rec = st.setdefault(vod, {"creator": creator.id, "done_to": 0, "parts": 0, "ended": False})
    spans = gta_spans(info, dur)
    if not spans:
        print(f"{creator.display_name}: live, no GTA chapter yet ({dur / 60:.0f} min recorded)")
        return False
    discover._mark_seen({vod})  # the live watcher owns this VOD: the normal scout leaves it alone
    _BUSY.append(1)
    ACTIVE.write_text(str(time.time()))
    chunk = next_chunk(spans, rec["done_to"], dur - SAFETY if still else dur, rec["parts"], still, cfg)
    if not chunk:
        if not still:
            rec["ended"] = True
            _save(st)
            _notify(f"{creator.display_name}'s stream ended: all {rec['parts']} parts clipped. "
                    "Building the long YouTube compilation.")
            _compile(vod)
        else:
            left = (rec["done_to"] or spans[0][0])
            print(f"{creator.display_name}: live, waiting for more GTA "
                  f"(done to {left / 60:.0f} min, recorded {dur / 60:.0f} min)")
        _save(st)
        return False
    url = part_url(vod, *chunk)
    print(f"\n=== {creator.display_name} LIVE part {rec['parts'] + 1}: "
          f"{chunk[0] / 60:.0f}-{chunk[1] / 60:.0f} min\n    {url}")
    try:
        ids = process(url, creator.id)
        _notify(f"{creator.display_name} live part {rec['parts'] + 1}: {len(ids)} clips made, "
                "GTA 6 ones go first in the queue.")
    except Exception as e:
        traceback.print_exc()
        _notify(f"{creator.display_name} live part failed: {str(e)[:120]}")
        rec["fail"] = rec.get("fail", 0) + 1
        if rec["fail"] < 3:
            _save(st)
            return False  # retry this chunk on the next run
    rec.update(done_to=chunk[1], parts=rec["parts"] + 1, fail=0)
    _save(st)
    _publish_soon()
    return True
    return False


def _compile(vod: str) -> None:
    """Agent 9: long 'best of' video for watch hours, right after the stream ends (GTA 6 streams only)."""
    from . import compilation
    try:
        compilation.build(vod)
    except Exception as e:
        traceback.print_exc()
        _notify(f"Compilation failed: {str(e)[:120]}")


def _publish_soon() -> None:
    """New clips are approved: run the publisher right away instead of waiting up to 15 min."""
    subprocess.run(["launchctl", "kickstart", f"gui/{__import__('os').getuid()}/com.gta6clipper.publish"],
                   check=False)


def active() -> bool:
    """A live stream is being clipped (checked by the hourly autopilot)."""
    try:
        return time.time() - float(ACTIVE.read_text()) < 3 * 3600
    except (OSError, ValueError):
        return False
