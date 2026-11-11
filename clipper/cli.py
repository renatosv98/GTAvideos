from __future__ import annotations

import argparse
import shutil
import sys

from . import db


def _doctor() -> int:
    import os

    from .config import ROOT, SECRETS, SETTINGS, load_creators

    ok = True

    def line(good: bool, msg: str) -> None:
        nonlocal ok
        ok &= good
        print(("  ✅ " if good else "  ❌ ") + msg)

    print("Tools")
    for t in ("ffmpeg", "ffprobe"):
        line(bool(shutil.which(t)), t)
    print("Keys")
    line(bool(os.environ.get("ANTHROPIC_API_KEY")), "ANTHROPIC_API_KEY in .env")
    if SETTINGS["platforms"]["youtube"]["enabled"]:
        line((SECRETS / "youtube_token.json").exists(), "YouTube authorised (clipper auth youtube)")
    if SETTINGS["platforms"]["tiktok"]["enabled"]:
        line((SECRETS / "tiktok_token.json").exists(), "TikTok authorised (clipper auth tiktok)")
    print("Creators")
    cs = load_creators()
    permitted = [c for c in cs if c.permitted]
    line(bool(permitted), f"{len(permitted)}/{len(cs)} creators with recorded permission")
    for c in cs:
        print(f"     - {c.id}: {'permitted' if c.permitted else 'NOT permitted'}")
    if SETTINGS["render"]["layout"] == "stack":
        d = ROOT / SETTINGS["render"]["bottom_dir"]
        n = len([p for p in d.iterdir() if p.suffix.lower() in (".mp4", ".mov", ".mkv", ".webm")]) if d.exists() else 0
        line(n > 0, f"{n} bottom videos of your own in {d.relative_to(ROOT)}")
    return 0 if ok else 1


def _status() -> int:
    from .scheduler import fmt_local

    with db.connect() as conn:
        print("Clips:", dict(conn.execute("SELECT status, COUNT(*) FROM clips GROUP BY status").fetchall()) or "none")
        rows = conn.execute("SELECT id, title, slot_at FROM clips WHERE status='approved' ORDER BY slot_at").fetchall()
        if rows:
            print("\nUpcoming:")
            for r in rows:
                print(f"  {fmt_local(r['slot_at'])}  #{r['id']}  {r['title']}")
        fails = conn.execute("SELECT clip_id, platform, error FROM uploads WHERE status='failed'").fetchall()
        for f in fails:
            print(f"  ❌ #{f['clip_id']} {f['platform']}: {f['error']}")
    return 0


def _rerender(ids: list[int]) -> int:
    import json
    from pathlib import Path

    from .config import DATA, load_creators
    from .render import render_clip

    creators = {c.id: c for c in load_creators()}
    with db.connect() as conn:
        q = ("SELECT clips.*, streams.video_path, streams.creator_id FROM clips JOIN streams ON streams.id = clips.stream_id ")
        rows = (conn.execute(q + f"WHERE clips.id IN ({','.join('?' * len(ids))})", ids).fetchall() if ids
                else conn.execute(q + "WHERE clips.status IN ('rendered','failed')").fetchall())
    for r in rows:
        print(f"#{r['id']} {r['title']}")
        try:
            path = render_clip(Path(r["video_path"]), r["start"], r["end"], json.loads(r["words_json"] or "[]"),
                               creators[r["creator_id"]], DATA / "clips" / f"{r['id']:05d}")
            status, err = ("rendered" if r["status"] in ("rendered", "failed") else r["status"]), None
        except Exception as e:
            path, status, err = None, "failed", str(e)
            print(f"   failed: {e}")
        with db.connect() as conn:
            conn.execute("UPDATE clips SET path=COALESCE(?, path), status=?, review_notes=? WHERE id=?",
                         (str(path) if path else None, status, err, r["id"]))
    print(f"Re-rendered {len(rows)} clips.")
    return 0


def _frame(url: str, at: float) -> int:
    import subprocess

    from .config import DATA
    from .download import probe

    with db.connect() as conn:
        row = conn.execute("SELECT video_path FROM streams WHERE url = ?", (url,)).fetchone()
    src = row["video_path"] if row and row["video_path"] else None
    if not src:  # not downloaded: read the frame straight from the stream
        info = probe(url)
        fmts = [f for f in info.get("formats", []) if f.get("vcodec") not in (None, "none") and (f.get("height") or 0) <= 720]
        src = (fmts[-1] if fmts else info)["url"]
    out = DATA / "frame.jpg"
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-ss", str(at), "-i", src, "-frames:v", "1",
                    "-vf", "scale=1280:-2,drawgrid=w=iw/10:h=ih/10:t=1:c=yellow@0.7", str(out)], check=True)
    print(f"Saved {out}. Each grid square is 10% of the width/height. Opening it...")
    subprocess.run(["open", str(out)])
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="clipper", description="GTA 6 stream -> shorts pipeline")
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("process", help="download a stream, find highlights, render shorts")
    s.add_argument("url")
    s.add_argument("--creator", help="creator id from config/creators.yaml (if auto-match fails)")

    s = sub.add_parser("check", help="only check whether a stream URL is allowed (no download)")
    s.add_argument("url")
    s.add_argument("--creator")

    s = sub.add_parser("review", help="open the review UI")
    s.add_argument("--port", type=int, default=5050)

    s = sub.add_parser("publish-due", help="upload clips whose slot is due (run every 15 min)")
    s.add_argument("--dry-run", action="store_true")

    s = sub.add_parser("rerender", help="re-render clips with the current layout settings (no new Claude call)")
    s.add_argument("ids", nargs="*", type=int, help="clip ids (default: all clips awaiting review)")

    s = sub.add_parser("frame", help="save a screenshot with a 10%% grid, to measure a creator's facecam position")
    s.add_argument("url")
    s.add_argument("--at", type=float, default=1800, help="seconds into the stream (default 1800)")

    sub.add_parser("status", help="queue and schedule overview")
    sub.add_parser("doctor", help="check setup")

    s = sub.add_parser("auth", help="authorise an upload platform")
    s.add_argument("platform", choices=["youtube", "tiktok"])

    a = p.parse_args(argv)

    if a.cmd in ("process", "check") and not a.url.startswith(("http://", "https://")):
        print(f"⛔ '{a.url}' is not a link. Paste the full URL of a stream recording, e.g.\n"
              "   clipper process \"https://www.twitch.tv/videos/1234567890\"")
        return 2

    if a.cmd == "process":
        from yt_dlp.utils import DownloadError

        from .config import PermissionError_
        from .pipeline import process
        try:
            ids = process(a.url, a.creator)
        except PermissionError_ as e:
            print(f"⛔ {e}")
            return 2
        except DownloadError as e:
            print(f"⛔ Couldn't read that link: {e}")
            return 2
        print(f"\nDone: {len(ids)} clips. Review them with: clipper review")
    elif a.cmd == "check":
        from .config import PermissionError_
        from .download import check_permission
        try:
            info, c = check_permission(a.url, a.creator)
            print(f"✅ {info.get('title')} -> {c.display_name} ({c.permission.get('source')})")
        except PermissionError_ as e:
            print(f"⛔ {e}")
            return 2
    elif a.cmd == "review":
        from .review_app import run
        run(a.port)
    elif a.cmd == "publish-due":
        from .scheduler import publish_due
        publish_due(a.dry_run)
    elif a.cmd == "rerender":
        return _rerender(a.ids)
    elif a.cmd == "frame":
        return _frame(a.url, a.at)
    elif a.cmd == "status":
        return _status()
    elif a.cmd == "doctor":
        return _doctor()
    elif a.cmd == "auth":
        if a.platform == "youtube":
            from .uploaders.youtube import auth
        else:
            from .uploaders.tiktok import auth
        auth()
    return 0


if __name__ == "__main__":
    sys.exit(main())
