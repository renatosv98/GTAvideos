from __future__ import annotations

from pathlib import Path
from typing import Any

import yt_dlp

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
    if SETTINGS["download"].get("chat"):
        opts.update({"writesubtitles": True, "subtitleslangs": ["live_chat"]})
    with yt_dlp.YoutubeDL(opts) as ydl:
        ydl.download([url])

    video = next((p for p in out_dir.glob("source.*") if p.suffix in (".mp4", ".mkv", ".webm")), None)
    if not video:
        raise RuntimeError(f"Download finished but no video found in {out_dir}")
    chat = next(out_dir.glob("*.live_chat.json"), None)
    return video, chat
