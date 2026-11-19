"""Agent 9 - Compilations: long horizontal YouTube videos ("lo mejor de ...") for watch hours (YPP: 4,000 h).

Shorts views never count as watch hours, so after each GTA 6 stream this agent:
1. takes the best clips already chosen from that stream (highest viral chance, ~11-13 min in total),
2. downloads ONLY those moments again from the VOD, horizontal 1080p (recordings are deleted after clipping),
3. edits them in stream order: intro card, a numbered caption per moment in our own words (our editorial layer,
   which also helps against the 'reused content' policy), permanent creator credit, loudness levelled,
4. writes title/description/tags with Claude, adds YouTube chapters (one per moment) and a thumbnail,
5. uploads to YouTube, scheduled for the evening prime time (Spain), counted against the daily upload cap.
"""
from __future__ import annotations

import json
import shutil
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path

import yt_dlp
from yt_dlp.utils import download_range_func

from . import db, llm
from .config import DATA, SETTINGS, load_creators
from .download import split_part

TARGET_S, MAX_S = 12 * 60, 14 * 60  # unverified channels can't upload over 15 min
PAD_BEFORE, PAD_AFTER = 5.0, 3.0    # a little context around each moment (long-form viewers want the setup)
W, H = 1920, 1080

SCHEMA = {
    "type": "object",
    "properties": {
        "title": {"type": "string"},
        "description": {"type": "string"},
        "tags": {"type": "array", "items": {"type": "string"}},
        "thumbnail_text": {"type": "string"},
        "chapter_titles": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["title", "description", "tags", "thumbnail_text", "chapter_titles"],
    "additionalProperties": False,
}


def _ensure_table() -> None:
    with db.connect() as conn:
        conn.execute("""CREATE TABLE IF NOT EXISTS compilations (
            id INTEGER PRIMARY KEY, source TEXT UNIQUE, title TEXT, clip_ids TEXT, path TEXT, youtube_id TEXT,
            publish_at TEXT, status TEXT, error TEXT, created_at TEXT DEFAULT (datetime('now')))""")


def moments(vod: str) -> list[dict]:
    """Approved/published clips of a VOD with absolute VOD times, best first."""
    out = []
    with db.connect() as conn:
        rows = conn.execute(
            "SELECT c.*, s.url AS surl, s.creator_id, s.title AS stitle FROM clips c JOIN streams s ON s.id=c.stream_id "
            "WHERE (s.url=? OR s.url LIKE ?) AND c.status IN ('approved','published')", (vod, vod + "#part=%")
        ).fetchall()
        for r in rows:
            _, part = split_part(r["surl"])
            off = part[0] if part else _range_offset(r["stream_id"])
            out.append({"id": r["id"], "a": off + r["start"], "b": off + r["end"], "score": r["score"] or 0,
                        "title": r["title"], "caption": r["caption"] or "", "creator_id": r["creator_id"],
                        "stream_title": r["stitle"] or ""})
    return sorted(out, key=lambda m: -m["score"])


def _range_offset(stream_id: int) -> float:
    """Normal (non-chunk) streams downloaded only their GTA part: clip times are relative to that range."""
    with db.connect() as conn:
        r = conn.execute("SELECT video_path FROM streams WHERE id=?", (stream_id,)).fetchone()
    try:
        return float(json.loads((Path(r["video_path"]).parent / "range.json").read_text())[0])
    except Exception:
        return 0.0


def plan(ms: list[dict]) -> list[dict]:
    """Best moments until ~12 min, then in stream order. Overlapping moments are merged."""
    chosen, total = [], 0.0
    for m in ms:
        d = (m["b"] - m["a"]) + PAD_BEFORE + PAD_AFTER
        if total + d > MAX_S:
            continue
        chosen.append(m)
        total += d
        if total >= TARGET_S:
            break
    chosen.sort(key=lambda m: m["a"])
    merged: list[dict] = []
    for m in chosen:
        a, b = max(0.0, m["a"] - PAD_BEFORE), m["b"] + PAD_AFTER
        if merged and a <= merged[-1]["b"]:
            merged[-1]["b"] = max(merged[-1]["b"], b)
            merged[-1]["ids"].append(m["id"])
            continue
        merged.append({**m, "a": a, "b": b, "ids": [m["id"]]})
    return merged


def _fetch(vod: str, a: float, b: float, out: Path) -> Path:
    h = SETTINGS["download"]["max_height"]
    opts = {"outtmpl": str(out.with_suffix(".%(ext)s")), "quiet": True, "noprogress": True,
            "format": f"bv*[height<={h}][ext=mp4]+ba[ext=m4a]/b[height<={h}]/bv*+ba/b",
            "merge_output_format": "mp4", "download_ranges": download_range_func(None, [(a, b)]),
            "concurrent_fragment_downloads": 8}
    with yt_dlp.YoutubeDL(opts) as y:
        y.download([vod])
    got = next((p for p in out.parent.glob(out.stem + ".*") if p.suffix in (".mp4", ".mkv", ".webm")), None)
    if not got:
        raise RuntimeError(f"segment {a:.0f}-{b:.0f}s did not download")
    return got


def _font() -> str:
    from .render import _font as f
    p = f()
    return f"fontfile='{p}':" if p else ""


def _ff(*args: str) -> None:
    from .render import render_ffmpeg  # the build with freetype (drawtext); Homebrew's ffmpeg has none
    r = subprocess.run([render_ffmpeg(), "-v", "error", "-y", *args], capture_output=True, text=True)
    if r.returncode:
        raise RuntimeError(f"ffmpeg: {r.stderr.strip()[-300:]}")


def _dur(p: Path) -> float:
    r = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(p)],
                       capture_output=True, text=True)
    return float(r.stdout.strip())


ENC = ["-c:v", "libx264", "-preset", "veryfast", "-crf", "20", "-pix_fmt", "yuv420p", "-r", "30",
       "-c:a", "aac", "-b:a", "160k", "-ar", "48000", "-ac", "2"]


def _card(text_file: Path, sub_file: Path, out: Path, seconds: float = 4.0) -> None:
    f = _font()
    _ff("-f", "lavfi", "-i", f"color=c=0x101418:s={W}x{H}:r=30:d={seconds}",
        "-f", "lavfi", "-i", f"anullsrc=r=48000:cl=stereo:d={seconds}",
        "-vf", f"drawtext={f}textfile='{text_file}':fontsize=84:fontcolor=white:x=(w-tw)/2:y=(h-th)/2-60,"
               f"drawtext={f}textfile='{sub_file}':fontsize=40:fontcolor=0xffd23f:x=(w-tw)/2:y=(h/2)+70",
        "-shortest", *ENC, str(out))


def _segment(src: Path, n: int, caption_file: Path, credit_file: Path, out: Path) -> None:
    f = _font()
    vf = (f"scale={W}:{H}:force_original_aspect_ratio=decrease,pad={W}:{H}:(ow-iw)/2:(oh-ih)/2,setsar=1,"
          f"drawtext={f}text='{n}':fontsize=64:fontcolor=black:box=1:boxcolor=0xffd23f:boxborderw=22:"
          f"x=60:y=h-200:enable='lt(t,7)',"
          f"drawtext={f}textfile='{caption_file}':fontsize=50:fontcolor=white:box=1:boxcolor=black@0.65:"
          f"boxborderw=20:x=160:y=h-196:enable='lt(t,7)',"
          f"drawtext={f}textfile='{credit_file}':fontsize=30:fontcolor=white@0.9:box=1:boxcolor=black@0.45:"
          f"boxborderw=10:x=w-tw-40:y=36")
    _ff("-i", str(src), "-vf", vf, "-af", "loudnorm=I=-14:TP=-1.5:LRA=11", *ENC, str(out))


def _wrap(text: str, width: int = 52) -> str:
    words, lines, cur = text.split(), [], ""
    for w in words:
        if len(cur) + len(w) + 1 > width and cur:
            lines.append(cur)
            cur = w
        else:
            cur = f"{cur} {w}".strip()
    lines.append(cur)
    return "\n".join(lines[:2])


def _ts(s: float) -> str:
    s = int(s)
    return f"{s // 3600}:{s // 60 % 60:02d}:{s % 60:02d}" if s >= 3600 else f"{s // 60:02d}:{s % 60:02d}"


def _publish_time(now: datetime | None = None) -> datetime | None:
    """Next 18:00 UK (19:00 Spain, long-form prime time); None = right away if that's under 1 h away."""
    from .scheduler import _tz
    now = now or datetime.now(timezone.utc)
    local = now.astimezone(_tz())
    t = local.replace(hour=18, minute=0, second=0, microsecond=0)
    if t <= local:
        t += timedelta(days=1)
    t = t.astimezone(timezone.utc)
    return None if t - now < timedelta(hours=1) else t


def thumbnail(video: Path, at: float, text_file: Path, out: Path) -> None:
    """1280x720 thumbnail: one frame of `video` at `at` seconds with huge text from `text_file`."""
    at = max(0.0, min(at, _dur(video) - 0.5))
    _ff("-ss", f"{at:.2f}", "-i", str(video), "-frames:v", "1",
        "-vf", f"scale=1280:720,drawtext={_font()}textfile='{text_file}':fontsize=110:fontcolor=white:borderw=9:"
               f"bordercolor=black:x=(w-tw)/2:y=h-th-60", str(out))
    if not out.exists() or out.stat().st_size < 5000:
        raise RuntimeError(f"thumbnail not written: {out}")


def build(vod: str, upload: bool = True, label: str = "") -> int:
    """Make (and by default upload) the compilation of one stream. Returns 0 on success."""
    _ensure_table()
    with db.connect() as conn:
        done = conn.execute("SELECT id, status FROM compilations WHERE source=?", (vod,)).fetchone()
    if done and done["status"] in ("uploaded", "scheduled") and upload:
        print(f"compilation of {vod} already uploaded (#{done['id']})")
        return 0
    ms = moments(vod)
    if len(ms) < 5:
        print(f"only {len(ms)} approved clips from this stream: not enough for a compilation")
        return 1
    segs = plan(ms)
    creators = {c.id: c for c in load_creators()}
    creator = creators[segs[0]["creator_id"]]
    total = sum(s["b"] - s["a"] for s in segs)
    print(f"agent 9 · compilation: {len(segs)} moments, {total / 60:.1f} min, from {vod}")

    work = DATA / "compilations" / (vod.rstrip("/").split("/")[-1] + (f"_{label}" if label else ""))
    work.mkdir(parents=True, exist_ok=True)

    # packaging first (cheap) so the chapter titles go on screen too
    listing = "\n".join(f"{i + 1}. {s['title']} — {s['caption'][:140]}" for i, s in enumerate(segs))
    meta = llm.ask_json(
        "You package long YouTube videos for GTAuploads, a Spanish GTA 6 clips channel (fan-made, creators gave "
        "permission). Write in natural Spain Spanish. Maximise click-through and watch time without clickbait lies.",
        f"Stream: {segs[0]['stream_title']} by {creator.display_name}.\nThe video is a {total / 60:.0f}-minute "
        f"compilation of these moments, in this order:\n{listing}\n\n"
        "Return: title (max 90 chars, e.g. 'Lo mejor de Rubius en su PRIMER DÍA de GTA 6'; include the creator and "
        "GTA 6), description (3-5 sentences: what happens, why it's worth watching, a question to comment; no "
        "hashtags, no links, no timestamps: chapters and credits are added by code), tags (12-20 search tags, "
        "Spanish + English), thumbnail_text (2-4 words, huge-text style, e.g. 'DÍA 1 EN GTA 6'), "
        f"chapter_titles (exactly {len(segs)} short titles, max 40 chars each, one per moment in order).",
        SCHEMA)
    chapters = (meta["chapter_titles"] + [s["title"][:40] for s in segs])[:len(segs)]

    credit = work / "credit.txt"
    credit.write_text(creator.credit.replace("(", "· ").replace(")", ""))
    parts, t = [], 0.0
    t1, t2 = work / "intro1.txt", work / "intro2.txt"
    t1.write_text(_wrap(meta["thumbnail_text"].upper(), 24))
    t2.write_text(f"Lo mejor de {creator.display_name} · clip fan no oficial")
    marks = []  # no intro card: viewers decide in the first seconds, so the video opens on the first moment
    starts = []
    for i, s in enumerate(segs, 1):
        print(f"    moment {i}/{len(segs)}: {s['a'] / 60:.1f}-{s['b'] / 60:.1f} min")
        raw = _fetch(vod, s["a"], s["b"], work / f"raw_{i:02d}")
        cap = work / f"cap_{i:02d}.txt"
        cap.write_text(_wrap(chapters[i - 1], 52))
        seg = work / f"{i:02d}.mp4"
        _segment(raw, i, cap, credit, seg)
        raw.unlink(missing_ok=True)
        marks.append((_ts(t), chapters[i - 1]))
        starts.append(t)
        parts.append(seg)
        t += _dur(seg)
    lst = work / "list.txt"
    lst.write_text("".join(f"file '{p}'\n" for p in parts))
    final = work / "compilation.mp4"
    _ff("-f", "concat", "-safe", "0", "-i", str(lst), "-c", "copy", "-movflags", "+faststart", str(final))
    for p in parts:
        p.unlink(missing_ok=True)

    # thumbnail: frame from the best moment + huge text
    bi = max(range(len(segs)), key=lambda k: segs[k]["score"])
    thumb = work / "thumb.jpg"
    thumbnail(final, starts[bi] + 8.5, t1, thumb)  # 8.5 s in: the on-screen caption (first 7 s) is gone

    desc = "\n".join([
        meta["description"].strip(), "",
        "🔔 Suscríbete para no perderte ningún clip de GTA 6 🔥", "",
        "Capítulos:", *[f"{a} {b}" for a, b in marks], "",
        f"Clips de {creator.credit}.",
        (creator.links or {}).get("description", ""),
        "Clip fan no oficial. No afiliado a Rockstar Games ni a " + creator.display_name + ".", "",
        f"#GTA6 #{creator.display_name.replace(' ', '')} #GTA6Español",
    ]).strip()
    title = meta["title"][:100]
    with db.connect() as conn:
        conn.execute("INSERT INTO compilations (source, title, clip_ids, path, status) VALUES (?,?,?,?, 'built') "
                     "ON CONFLICT(source) DO UPDATE SET title=excluded.title, clip_ids=excluded.clip_ids, "
                     "path=excluded.path, status='built', error=NULL",
                     (vod, title, json.dumps([i for s in segs for i in s["ids"]]), str(final)))
    print(f"    built {final} ({_dur(final) / 60:.1f} min)\n    title: {title}")
    (work / "description.txt").write_text(desc)
    tags = list(dict.fromkeys(["GTA 6", "GTA6", "GTA VI", creator.display_name, f"{creator.display_name} GTA 6",
                               "GTA 6 español", *meta["tags"]]))
    (work / "tags.json").write_text(json.dumps(tags, ensure_ascii=False))
    if not upload:
        return 0
    with db.connect() as conn:
        conn.execute("UPDATE compilations SET status='ready' WHERE source=?", (vod,))
    upload_ready()
    return 0


def upload_ready() -> list[str]:
    """Upload every built compilation still waiting (e.g. YouTube's daily cap was full). Called by the publisher
    every 15 min, before Shorts: a long video is worth more watch hours than any single Short."""
    _ensure_table()
    out = []
    with db.connect() as conn:
        rows = conn.execute("SELECT * FROM compilations WHERE status IN ('ready','waiting_cap') "
                            "AND youtube_id IS NULL").fetchall()
    for r in rows:
        final = Path(r["path"])
        work = final.parent
        if not final.exists():
            with db.connect() as conn:
                conn.execute("UPDATE compilations SET status='failed', error='video file missing' WHERE id=?",
                             (r["id"],))
            continue
        from .uploaders import youtube
        when = _publish_time()
        try:
            vid = youtube.upload(final, r["title"], (work / "description.txt").read_text(), when,
                                 json.loads((work / "tags.json").read_text()))
        except Exception as e:
            cap = "uploadLimitExceeded" in str(e)
            with db.connect() as conn:
                conn.execute("UPDATE compilations SET status=?, error=? WHERE id=?",
                             ("waiting_cap" if cap else "ready", str(e)[:500], r["id"]))
            out.append(f"compilation #{r['id']}: " + ("YouTube daily limit, will retry" if cap else f"failed: {e}"))
            if cap:
                from .scheduler import _yt_cap_hit
                _yt_cap_hit()
                break
            continue
        from .scheduler import _yt_cap_clear
        _yt_cap_clear()
        try:
            from googleapiclient.http import MediaFileUpload
            youtube._api().thumbnails().set(videoId=vid, media_body=MediaFileUpload(str(work / "thumb.jpg"))).execute()
        except Exception as e:
            print(f"    thumbnail not set (needs a phone-verified channel): {str(e)[:120]}")
        with db.connect() as conn:
            conn.execute("UPDATE compilations SET youtube_id=?, publish_at=?, status=?, error=NULL, "
                         "created_at=datetime('now') WHERE id=?",
                         (vid, when.isoformat() if when else None, "scheduled" if when else "uploaded", r["id"]))
        final.unlink(missing_ok=True)  # keep the disk free; YouTube has it
        out.append(f"compilation #{r['id']} uploaded: https://youtu.be/{vid}"
                   + (f" (public {when:%a %d %b %H:%M} UTC)" if when else ""))
    return out


def uploads_last_24h() -> int:
    """Counted against YouTube's daily upload cap by the publisher."""
    _ensure_table()
    with db.connect() as conn:
        return conn.execute("SELECT COUNT(*) FROM compilations WHERE youtube_id IS NOT NULL "
                            "AND created_at > datetime('now','-1 day')").fetchone()[0]
