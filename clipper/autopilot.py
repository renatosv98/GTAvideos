"""Autopilot (launchd, every 3 h): new streams -> clips -> QA -> auto-approve; daily analyst; disk housekeeping.
Publishing runs separately every 15 min (`clipper publish-due`)."""
from __future__ import annotations

import fcntl
import shutil
import subprocess
import traceback

from . import db, discover, health
from .agents import analyst
from .config import DATA, SETTINGS


def _cfg() -> dict:
    return SETTINGS.get("autopilot", {})


def _free_gb() -> float:
    return shutil.disk_usage(DATA).free / 1e9


def cleanup() -> None:
    """Delete downloaded recordings except the newest `keep_streams` (clips that need them are done by then)."""
    keep = int(_cfg().get("keep_streams", 3))
    with db.connect() as conn:
        rows = conn.execute("SELECT id, video_path FROM streams WHERE status='rendered' AND video_path IS NOT NULL "
                            "ORDER BY id DESC").fetchall()
        for r in rows[keep:]:
            d = (DATA / "streams" / r["video_path"].split("/streams/")[-1]).parent
            if d.is_dir() and d.parent == DATA / "streams":
                for f in d.glob("source.*"):
                    f.unlink()
                print(f"  cleanup: deleted recording of stream #{r['id']} ({d.name})")
            conn.execute("UPDATE streams SET video_path=NULL WHERE id=?", (r["id"],))


def _notify(msg: str) -> None:
    subprocess.run(["osascript", "-e", f'display notification "{msg}" with title "GTAuploads autopilot"'],
                   check=False)


def run() -> int:
    DATA.mkdir(parents=True, exist_ok=True)
    with open(DATA / "autopilot.lock", "w") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            print("autopilot already running; skipping")
            return 0
        from .pipeline import process

        cleanup()
        made = 0
        if _free_gb() < float(_cfg().get("min_free_gb", 40)):
            _notify(f"Disk almost full ({_free_gb():.0f} GB free): not downloading new streams.")
        else:
            print("scout: looking for new GTA streams ...")
            for creator, url, title in discover.find_new()[: int(_cfg().get("max_streams_per_run", 2))]:
                print(f"\n=== {creator.display_name}: {title[:80]}\n    {url}")
                try:
                    made += len(process(url, creator.id))
                except Exception as e:
                    traceback.print_exc()
                    _notify(f"{creator.display_name} stream failed: {str(e)[:120]}")
                cleanup()
            if made:
                with db.connect() as conn:
                    ok = conn.execute("SELECT COUNT(*) FROM clips WHERE status='approved' AND review_notes LIKE "
                                      "'auto-approved%' AND created_at > datetime('now','-1 day')").fetchone()[0]
                _notify(f"{made} new clips made, {ok} auto-approved and scheduled.")
        if analyst.due():
            try:
                analyst.run()
            except Exception:
                traceback.print_exc()
        from .agents import growth
        if growth.due():
            try:
                growth.run()
            except Exception:
                traceback.print_exc()
        from .agents import growth as _g, manager
        if _g.research_due():  # market research (web) every ~3 days, before packaging uses it
            try:
                _g.research()
            except Exception:
                traceback.print_exc()
        try:  # every cycle: YouTube status sync + packaging for queued clips
            for note in manager.reconcile():
                print(f"  manager: {note}")
        except Exception:
            traceback.print_exc()
        if manager.due():  # last: reviews what everyone else just did
            try:
                manager.run()
            except Exception:
                traceback.print_exc()
        health.notify(health.check())
    return 0
