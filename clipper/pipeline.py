"""Orchestrator: permission check -> download -> agent 1 (transcriber) -> agent 2 (selector) -> agent 3 (producer).
Publishing (agent 4) runs on its own schedule (`clipper publish-due`); the analyst (agent 5) runs daily.
Every step saves its output, so re-running the same link resumes where it stopped."""
from __future__ import annotations

from pathlib import Path

from . import db
from .agents import producer, selector, transcriber
from .download import check_permission, download


def process(url: str, creator_id: str | None = None, reselect: bool = False) -> list[int]:
    print("permission: checking channel against config/creators.yaml ...")
    info, creator = check_permission(url, creator_id)
    print(f"    OK: {creator.display_name} (permission: {creator.permission.get('source')})")

    with db.connect() as conn:
        row = conn.execute("SELECT * FROM streams WHERE url = ?", (url,)).fetchone()
        if row and row["status"] == "rendered" and not reselect:
            print(f"    already processed (stream #{row['id']}). Use --reselect to pick clips again.")
            return []
        if not row:
            conn.execute("INSERT INTO streams (url, creator_id, title, duration) VALUES (?,?,?,?)",
                         (url, creator.id, info.get("title"), info.get("duration")))
        stream_id = conn.execute("SELECT id FROM streams WHERE url = ?", (url,)).fetchone()["id"]

    try:
        print("download ...")
        video, chat = download(url, info)
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

        print(f"agent 3 · producer: {len(picks)} shorts ...")
        ids = producer.run(stream_id, Path(video), creator, picks)
        with db.connect() as conn:
            conn.execute("UPDATE streams SET status='rendered' WHERE id=?", (stream_id,))
        return ids
    except Exception as e:
        with db.connect() as conn:
            conn.execute("UPDATE streams SET status='failed', error=? WHERE id=?", (str(e), stream_id))
        raise
