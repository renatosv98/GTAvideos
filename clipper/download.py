from __future__ import annotations

import re

from pathlib import Path
from typing import Any

import yt_dlp
from yt_dlp.utils import download_range_func

from .config import DATA, SETTINGS, Creator, resolve_creator


def probe(url: str) -> dict[str, Any]:
    with yt_dlp.YoutubeDL({"quiet": True, "skip_download": True, "noplaylist": True}) as ydl:
        return ydl.extract_info(url, download=False)


def check_permission(url: str, creator_id: str | None = None) -> tuple[dict[str, Any], Creator]:
    """Metadata-only lookup + allowlist check. Nothing is downloaded if this raises."""
    info = probe(url)
    if info.get("is_live"):
        raise RuntimeError("Stream is still live. Run again once the VOD is available.")
    return info, resolve_creator(info, creator_id)


GTA_WORDS = ("grand theft auto", "gta")


def gta_minutes(info: dict[str, Any]) -> float | None:
    """Minutes of GTA according to the recording's game chapters (Twitch); None if it has no chapters."""
    ch = info.get("chapters") or []
    if not ch:
        return None
    return sum(c["end_time"] - c["start_time"] for c in ch
               if any(w in (c.get("title") or "").lower() for w in GTA_WORDS)) / 60


GTA6_RE = re.compile(r"grand theft auto:? (vi|6)\b|\bgta ?(vi|6)\b", re.I)


def is_gta6(info: dict[str, Any], since: str = "2026-11-18") -> bool:
    """GTA 6 gameplay? Twitch: 20+ min in a 'Grand Theft Auto VI' chapter. No chapters (YouTube): 'GTA 6' in the
    title of a video published on/after GTA 6 came out (before that, 'GTA 6' titles were GTA 5 countdown content)."""
    ch = info.get("chapters") or []
    if ch:
        return sum(c["end_time"] - c["start_time"] for c in ch if GTA6_RE.search(c.get("title") or "")) >= 20 * 60
    d = info.get("upload_date") or ""
    return bool(GTA6_RE.search(info.get("title") or "")) and f"{d[:4]}-{d[4:6]}-{d[6:]}" >= since


def gta_range(info: dict[str, Any]) -> tuple[float, float] | None:
    """(start, end) seconds spanning all GTA chapters, or None to take the whole recording."""
    ch = [c for c in (info.get("chapters") or []) if any(w in (c.get("title") or "").lower() for w in GTA_WORDS)]
    if not ch:
        return None
    a, b = min(c["start_time"] for c in ch), max(c["end_time"] for c in ch)
    a, b = max(0.0, a - 60), b + 60  # a minute of margin either side
    dur = info.get("duration") or b
    return None if (b - a) > 0.9 * dur else (a, min(b, dur))


def download(url: str, info: dict[str, Any]) -> tuple[Path, Path | None]:
    vid = info["id"]
    out_dir = DATA / "streams" / vid
    out_dir.mkdir(parents=True, exist_ok=True)
    h = SETTINGS["download"]["max_height"]
    opts: dict[str, Any] = {
        "outtmpl": str(out_dir / "source.%(ext)s"),
        "format": f"bv*[height<={h}][ext=mp4]+ba[ext=m4a]/b[height<={h}]/bv*+ba/b",
        "merge_output_format": "mp4",
        "noplaylist": True,
        "quiet": False,
        "concurrent_fragment_downloads": 8,
    }
    rng = gta_range(info)
    if rng and not any(out_dir.glob("source.*")):
        # only the GTA part of a mixed stream: much faster, much less disk
        print(f"    downloading only the GTA part: {rng[0] / 3600:.1f}h-{rng[1] / 3600:.1f}h")
        opts["download_ranges"] = download_range_func(None, [rng])
        (out_dir / "range.json").write_text(f"[{rng[0]}, {rng[1]}]")
    if SETTINGS["download"].get("chat"):
        opts.update({"writesubtitles": True, "subtitleslangs": ["live_chat"]})
    with yt_dlp.YoutubeDL(opts) as ydl:
        ydl.download([url])

    video = next((p for p in out_dir.glob("source.*") if p.suffix in (".mp4", ".mkv", ".webm")), None)
    if not video:
        raise RuntimeError(f"Download finished but no video found in {out_dir}")
    chat = next(out_dir.glob("*.live_chat.json"), None)
    return video, chat
