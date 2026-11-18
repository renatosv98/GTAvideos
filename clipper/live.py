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
from .download import GTA_WORDS, part_url, probe

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
    """GTA chapters as (start, end); the chapter in progress (end == start on a live VOD) runs to now."""
    out = []
    for c in info.get("chapters") or []:
        if any(w in (c.get("title") or "").lower() for w in GTA_WORDS):
            end = c["end_time"] if c["end_time"] > c["start_time"] else now_dur
            out.append((float(c["start_time"]), float(end)))
    return out


def next_chunk(spans, done_to: float, edge: float, n_done: int, live: bool, cfg: dict) -> tuple[float, float] | None:
    """Next part to process: from where we stopped to the recorded edge, over the GTA chapters.
    While live, wait for a full chunk; once ended, take any remainder of 5+ min."""
    if not spans:
        return None
    a = max(done_to, min(s for s, _ in spans))
    b = min(edge, max(e for _, e in spans))
    gta = sum(max(0.0, min(e, b) - max(s, a)) for s, e in spans)
    need = 60 * float(cfg.get("first_chunk_min" if n_done == 0 else "chunk_min", 45 if n_done == 0 else 60))
    if live and gta < need:
        return None
    if not live and gta < 300:
        return None
    b = min(b, a + 60 * float(cfg.get("max_chunk_min", 90)))  # fell behind: keep chunks quick to turn around
    return (a, b)


def watch() -> int:
    DATA.mkdir(parents=True, exist_ok=True)
    with open(DATA / "live.lock", "w") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            print("live watcher already running")
            return 0
        from .pipeline import process
        raw = {c["id"]: c for c in _load("creators.yaml").get("creators", [])}
        st = _state()
        busy = False
        for creator in load_creators():
            cfg = raw.get(creator.id, {})
            if not (creator.permitted and cfg.get("live_chunks") and cfg.get("vods") and cfg.get("live")):
                continue
            live = discover._is_live(cfg["live"])
            try:
                newest = discover._recent(cfg["vods"])[:1]
            except Exception as e:
                print(f"{creator.display_name}: could not list recordings: {e}")
                continue
            if not newest:
                continue
            vod = discover._clean(newest[0].get("url") or "")
            rec = st.get(vod)
            if not live and (not rec or rec.get("ended")):
                continue  # not live, and not a stream we were clipping live
            while True:
                info = probe(vod)
                still = bool(info.get("is_live"))
                dur = float(info.get("duration") or 0)
                rec = st.setdefault(vod, {"creator": creator.id, "done_to": 0, "parts": 0, "ended": False})
                spans = gta_spans(info, dur)
                if not spans:
                    print(f"{creator.display_name}: live, no GTA chapter yet ({dur / 60:.0f} min recorded)")
                    break
                discover._mark_seen({vod})  # the live watcher owns this VOD: the normal scout leaves it alone
                busy = True
                ACTIVE.write_text(str(time.time()))
                chunk = next_chunk(spans, rec["done_to"], dur - SAFETY if still else dur, rec["parts"], still, cfg)
                if not chunk:
                    if not still:
                        rec["ended"] = True
                        _notify(f"{creator.display_name}'s stream ended: all {rec['parts']} parts clipped.")
                    else:
                        left = (rec["done_to"] or spans[0][0])
                        print(f"{creator.display_name}: live, waiting for more GTA "
                              f"(done to {left / 60:.0f} min, recorded {dur / 60:.0f} min)")
                    _save(st)
                    break
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
                        break  # retry this chunk on the next run
                rec.update(done_to=chunk[1], parts=rec["parts"] + 1, fail=0)
                _save(st)
                _publish_soon()
        if not busy:
            ACTIVE.unlink(missing_ok=True)
    return 0


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
