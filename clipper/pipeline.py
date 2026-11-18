"""Orchestrator: permission check -> download -> agent 1 (transcriber) -> agent 2 (selector) -> agent 3 (producer).
Publishing (agent 4) runs on its own schedule (`clipper publish-due`); the analyst (agent 5) runs daily.
Every step saves its output, so re-running the same link resumes where it stopped."""
from __future__ import annotations

from datetime import datetime, timezone

from pathlib import Path

from . import db
from .agents import producer, selector, transcriber
from .download import check_permission, download


def process(url: str, creator_id: str | None = None, reselect: bool = False) -> list[int]:
    from .download import is_gta6, split_part
    base, part = split_part(url)
    print("permission: checking channel against config/creators.yaml ...")
    info, creator = check_permission(base, creator_id, allow_live=part is not None)
    gta6 = int(is_gta6(info))
    if part:  # one chunk of a (possibly still live) stream: its own stream row, folder and duration
        info = {**info, "id": f"{info['id']}_p{int(part[0])}", "duration": part[1] - part[0], "chapters": None,
                "heatmap": None, "is_live": False}
    print(f"    OK: {creator.display_name} (permission: {creator.permission.get('source')})")

    with db.connect() as conn:
        row = conn.execute("SELECT * FROM streams WHERE url = ?", (url,)).fetchone()
        if row and row["status"] == "rendered" and not reselect:
            print(f"    already processed (stream #{row['id']}). Use --reselect to pick clips again.")
            return []
        if not row:
            ts = info.get("timestamp") or info.get("release_timestamp")
            if ts:
                rec = datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
            elif info.get("upload_date"):
                d = info["upload_date"]; rec = f"{d[:4]}-{d[4:6]}-{d[6:]} 00:00:00"
            else:
                rec = None
            conn.execute("INSERT INTO streams (url, creator_id, title, duration, recorded_at, gta6) VALUES (?,?,?,?,?,?)",
                         (url, creator.id, info.get("title"), info.get("duration"), rec, gta6))
        stream_id = conn.execute("SELECT id FROM streams WHERE url = ?", (url,)).fetchone()["id"]

    try:
        print("download ...")
        video, chat = download(base, info, part)
        stream_dir = Path(video).parent
        with db.connect() as conn:
            conn.execute("UPDATE streams SET video_path=?, chat_path=?, status='downloaded', error=NULL WHERE id=?",
                         (str(video), str(chat) if chat else None, stream_id))

        print("agent 1 · transcriber ...")
        report = transcriber.run(Path(video), chat, info, creator, stream_dir)

        print("agent 2 · selector (Claude) ...")
        picks = selector.run(Path(video), report, creator, stream_dir, redo=reselect)
        with db.connect() as conn:
            conn.execute("UPDATE streams SET status='analyzed' WHERE id=?", (stream_id,))

        if part:  # never clip the same minutes of a VOD twice (overlapping live chunks / re-runs)
            taken = clipped_ranges(base)
            before = len(picks)
            picks = [p for p in picks if not any(min(part[0] + p["end"], b) - max(part[0] + p["start"], a) > 5
                                                 for a, b in taken)]
            if len(picks) < before:
                print(f"    dropped {before - len(picks)} moment(s) already clipped from this VOD")

        print(f"agent 3 · producer: {len(picks)} shorts ...")
        ids = producer.run(stream_id, Path(video), creator, picks)
        with db.connect() as conn:
            conn.execute("UPDATE streams SET status='rendered' WHERE id=?", (stream_id,))
        return ids
    except Exception as e:
        with db.connect() as conn:
            conn.execute("UPDATE streams SET status='failed', error=? WHERE id=?", (str(e), stream_id))
        raise


def clipped_ranges(vod: str) -> list[tuple[float, float]]:
    """Absolute VOD seconds of every non-rejected clip already made from chunks of this VOD."""
    from .download import split_part
    out = []
    with db.connect() as conn:
        for r in conn.execute("SELECT c.start, c.end, s.url FROM clips c JOIN streams s ON s.id=c.stream_id "
                              "WHERE s.url LIKE ? AND c.status NOT IN ('rejected','failed')", (vod + "#part=%",)):
            _, part = split_part(r["url"])
            if part:
                out.append((part[0] + r["start"], part[0] + r["end"]))
    return out
