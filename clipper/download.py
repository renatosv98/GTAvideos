from __future__ import annotations

import re

from pathlib import Path
from typing import Any

import yt_dlp
from yt_dlp.utils import download_range_func

from .config import DATA, SETTINGS, Creator, resolve_creator


PART_RE = re.compile(r"#part=(\d+)-(\d+)$")


def split_part(url: str) -> tuple[str, tuple[float, float] | None]:
    """'<vod>#part=A-B' (a chunk of a stream that may still be live) -> (vod url, (A, B) seconds)."""
    m = PART_RE.search(url)
    return (url[:m.start()], (float(m.group(1)), float(m.group(2)))) if m else (url, None)


def part_url(vod: str, a: float, b: float) -> str:
    return f"{vod}#part={int(a)}-{int(b)}"


def probe(url: str) -> dict[str, Any]:
    with yt_dlp.YoutubeDL({"quiet": True, "skip_download": True, "noplaylist": True}) as ydl:
        return ydl.extract_info(url, download=False)


def check_permission(url: str, creator_id: str | None = None,
                     allow_live: bool = False) -> tuple[dict[str, Any], Creator]:
    """Metadata-only lookup + allowlist check. Nothing is downloaded if this raises.
    allow_live: a finished chunk of a stream still in progress (live mode) may be taken."""
    info = probe(url)
    if info.get("is_live") and not allow_live:
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
        now = float(info.get("duration") or 0)  # live VOD: the chapter in progress has end == start; it runs to now
        return sum((c["end_time"] if c["end_time"] > c["start_time"] else max(now, c["start_time"])) - c["start_time"]
                   for c in ch if GTA6_RE.search(c.get("title") or "")) >= 20 * 60
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


VIDEO_EXT = (".mp4", ".mkv", ".webm")


def _have_source(out_dir: Path) -> Path | None:
    """A finished source video in out_dir (half-written .part / .ytdl / format pieces don't count)."""
    return next((p for p in sorted(out_dir.glob("source.*"))
                 if p.suffix in VIDEO_EXT and not re.search(r"\.f\d+\.", p.name)), None)


def _clear_partial(out_dir: Path) -> None:
    for p in out_dir.glob("source.*"):
        if p.suffix not in VIDEO_EXT or re.search(r"\.f\d+\.", p.name):
            p.unlink(missing_ok=True)


def finished_playlist(text: str, base: str) -> str:
    """A live recording's HLS playlist rewritten as a finished one (absolute segment URLs + ENDLIST).
    ffmpeg reads a playlist without ENDLIST as a live broadcast, at playback speed (1x); marked finished it
    downloads as fast as the connection allows (~8x)."""
    from urllib.parse import urljoin
    out = []
    for ln in text.splitlines():
        ln = ln.strip()
        if not ln or ln == "#EXT-X-ENDLIST" or ln.startswith("#EXT-X-PLAYLIST-TYPE"):
            continue
        if ln.startswith("#EXT-X-MAP:"):
            ln = re.sub(r'URI="([^"]+)"', lambda m: f'URI="{urljoin(base, m.group(1))}"', ln)
        elif not ln.startswith("#"):
            ln = urljoin(base, ln)
        out.append(ln)
        if ln == "#EXTM3U":
            out.append("#EXT-X-PLAYLIST-TYPE:VOD")
    return "\n".join(out + ["#EXT-X-ENDLIST", ""])


def _fast_part(url: str, part: tuple[float, float], out_dir: Path) -> Path | None:
    """Download [a, b] of a Twitch recording that is still live, at full speed. None if not possible
    (the caller then uses the normal yt-dlp path)."""
    import shutil
    import subprocess
    import urllib.request
    h = SETTINGS["download"]["max_height"]
    try:
        with yt_dlp.YoutubeDL({"quiet": True, "no_warnings": True, "format": f"b[height<={h}]/b"}) as ydl:
            m3u8 = ydl.extract_info(url, download=False).get("url") or ""
        if ".m3u8" not in m3u8:
            return None
        req = urllib.request.Request(m3u8, headers={"User-Agent": "Mozilla/5.0"})
        text = urllib.request.urlopen(req, timeout=30).read().decode()
        pl = out_dir / "part.m3u8"
        pl.write_text(finished_playlist(text, m3u8))
        video = out_dir / "source.mp4"
        a, b = part
        r = subprocess.run([shutil.which("ffmpeg") or "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
                            "-protocol_whitelist", "file,http,https,tcp,tls,crypto",
                            "-ss", f"{a:.2f}", "-i", str(pl), "-t", f"{b - a:.2f}", "-c", "copy",
                            "-movflags", "+faststart", str(video)], capture_output=True, text=True, timeout=3 * 3600)
        got = _media_seconds(video) if r.returncode == 0 and video.exists() else 0
        if got < 0.9 * (b - a):
            print(f"    fast download incomplete ({got:.0f}s of {b - a:.0f}s): {r.stderr.strip()[-200:]}")
            video.unlink(missing_ok=True)
            return None
        return video
    except Exception as e:
        print(f"    fast download not possible ({str(e)[:150]}), using the normal path")
        return None


def _media_seconds(path: Path) -> float:
    import json
    import shutil
    import subprocess
    r = subprocess.run([shutil.which("ffprobe") or "ffprobe", "-v", "error", "-show_entries", "format=duration",
                        "-of", "json", str(path)], capture_output=True, text=True)
    try:
        return float(json.loads(r.stdout)["format"]["duration"])
    except (ValueError, KeyError):
        return 0.0


def download(url: str, info: dict[str, Any], part: tuple[float, float] | None = None) -> tuple[Path, Path | None]:
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
    rng = part or gta_range(info)
    have = _have_source(out_dir)
    if not have:
        _clear_partial(out_dir)  # an interrupted download restarts cleanly, still only the part
    if part and not have:
        print(f"    live chunk: {part[0] / 3600:.2f}h-{part[1] / 3600:.2f}h")
        if "twitch.tv/videos/" in url and _fast_part(url, part, out_dir):
            (out_dir / "range.json").write_text(f"[{part[0]}, {part[1]}]")
            return out_dir / "source.mp4", None
        opts["download_ranges"] = download_range_func(None, [part])
        (out_dir / "range.json").write_text(f"[{part[0]}, {part[1]}]")
    elif rng and not have:
        # only the GTA part of a mixed stream: much faster, much less disk
        print(f"    downloading only the GTA part: {rng[0] / 3600:.1f}h-{rng[1] / 3600:.1f}h")
        opts["download_ranges"] = download_range_func(None, [rng])
        (out_dir / "range.json").write_text(f"[{rng[0]}, {rng[1]}]")
    if SETTINGS["download"].get("chat"):
        opts.update({"writesubtitles": True, "subtitleslangs": ["live_chat"]})
    if not have:
        with yt_dlp.YoutubeDL(opts) as ydl:
            ydl.download([url])

    video = _have_source(out_dir)
    if not video:
        raise RuntimeError(f"Download finished but no video found in {out_dir}")
    chat = next(out_dir.glob("*.live_chat.json"), None)
    return video, chat
