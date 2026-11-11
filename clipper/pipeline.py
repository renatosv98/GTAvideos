from __future__ import annotations

import json
from pathlib import Path

from . import db
from .analyze import analyze
from .config import DATA
from .download import check_permission, download
from .render import render_clip
from .signals import audio_loudness, chat_activity, find_candidates


def process(url: str, creator_id: str | None = None) -> list[int]:
    print("1/5 checking channel against config/creators.yaml ...")
    info, creator = check_permission(url, creator_id)
    print(f"    OK: {creator.display_name} (permission: {creator.permission.get('source')})")

    with db.connect() as conn:
        row = conn.execute("SELECT * FROM streams WHERE url = ?", (url,)).fetchone()
        if row and row["status"] == "rendered":
            print(f"    already processed (stream #{row['id']}). Nothing to do.")
            return []
        if not row:
            conn.execute("INSERT INTO streams (url, creator_id, title, duration) VALUES (?,?,?,?)",
                         (url, creator.id, info.get("title"), info.get("duration")))
        stream_id = conn.execute("SELECT id FROM streams WHERE url = ?", (url,)).fetchone()["id"]

    try:
        print("2/5 downloading ...")
        video, chat = download(url, info)
        with db.connect() as conn:
            conn.execute("UPDATE streams SET video_path=?, chat_path=?, status='downloaded' WHERE id=?",
                         (str(video), str(chat) if chat else None, stream_id))

        print("3/5 scanning audio + chat for spikes ...")
        loud = audio_loudness(video)
        chat_arr = chat_activity(chat, len(loud))
        print(f"    {len(loud)/3600:.1f}h of audio, chat replay: {'yes' if chat_arr is not None else 'no'}")
        cands = find_candidates(loud, chat_arr)

        print(f"4/5 asking Claude to pick from {len(cands)} candidates ...")
        picks = analyze(video, info.get("title", ""), creator, cands)

        print(f"5/5 rendering {len(picks)} shorts ...")
        clip_ids = []
        for p in picks:
            with db.connect() as conn:
                cur = conn.execute(
                    "INSERT INTO clips (stream_id, start, end, score, title, caption, hashtags, reason, risk_notes, words_json)"
                    " VALUES (?,?,?,?,?,?,?,?,?,?)",
                    (stream_id, p["start"], p["end"], p["virality"], p["title"], p["caption"],
                     " ".join(h if h.startswith("#") else f"#{h}" for h in p["hashtags"]),
                     p["reason"], p["risk_notes"], json.dumps(p["words"])),
                )
                clip_id = cur.lastrowid
            out_dir = DATA / "clips" / f"{clip_id:05d}"
            try:
                path = render_clip(Path(video), p["start"], p["end"], p["words"], creator, out_dir)
                status, err = "rendered", None
            except Exception as e:  # keep going with the other clips
                path, status, err = None, "failed", str(e)
                print(f"    clip {clip_id} failed: {e}")
            with db.connect() as conn:
                conn.execute("UPDATE clips SET path=?, status=?, review_notes=? WHERE id=?",
                             (str(path) if path else None, status, err, clip_id))
            clip_ids.append(clip_id)
            print(f"    #{clip_id} [{p['virality']}/10] {p['title']}")

        with db.connect() as conn:
            conn.execute("UPDATE streams SET status='rendered' WHERE id=?", (stream_id,))
        return clip_ids
    except Exception as e:
        with db.connect() as conn:
            conn.execute("UPDATE streams SET status='failed', error=? WHERE id=?", (str(e), stream_id))
        raise
