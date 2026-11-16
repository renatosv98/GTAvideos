"""Agent 0 - Scout: find new GTA streams from the permitted creators.

For each creator with `vods` in creators.yaml, list their latest recordings and return the newest ones that
- are about GTA (title contains one of the creator's `keywords`),
- are finished (the newest recording is skipped while the creator is live: it is still growing),
- were not processed or skipped before.
"""
from __future__ import annotations

import json
import re

import yt_dlp

from . import db
from .config import DATA, Creator, load_creators

SEEN = DATA / "seen_vods.json"
DEFAULT_KEYWORDS = ["gta", "grand theft auto", "los santos", "vice city", "nopixel"]
LOOKBACK = 4  # only consider each creator's latest N recordings
MIN_GTA_MINUTES = 20


class _Quiet:
    def debug(self, msg): pass
    def info(self, msg): pass
    def warning(self, msg): pass
    def error(self, msg): pass


def _gta_minutes(url: str) -> float | None:
    from .download import gta_minutes
    try:
        with yt_dlp.YoutubeDL({"quiet": True, "logger": _Quiet(), "socket_timeout": 30}) as y:
            return gta_minutes(y.extract_info(url, download=False, process=False))
    except Exception:
        return None


def _seen() -> set[str]:
    try:
        return set(json.loads(SEEN.read_text()))
    except (OSError, ValueError):
        return set()


def _mark_seen(urls: set[str]) -> None:
    SEEN.write_text(json.dumps(sorted(_seen() | urls), indent=1))


def _clean(url: str) -> str:
    return url.split("#__youtubedl_smuggle")[0]


def _is_live(url: str) -> bool:
    try:
        with yt_dlp.YoutubeDL({"quiet": True, "logger": _Quiet(), "socket_timeout": 20}) as y:
            return bool(y.extract_info(url, download=False, process=False).get("is_live"))
    except Exception:
        return False  # "not currently live" is reported as an error


def _recent(url: str) -> list[dict]:
    with yt_dlp.YoutubeDL({"quiet": True, "logger": _Quiet(), "extract_flat": True, "socket_timeout": 30,
                           "playlistend": LOOKBACK}) as y:
        info = y.extract_info(url, download=False)
    return [e for e in (info.get("entries") or []) if e][:LOOKBACK]


def find_new(raw_creators: dict | None = None) -> list[tuple[Creator, str, str]]:
    """[(creator, vod_url, title)] newest first, at most one per creator."""
    from .config import _load
    raw = {c["id"]: c for c in (raw_creators or _load("creators.yaml")).get("creators", [])}
    with db.connect() as conn:
        done = {r["url"] for r in conn.execute("SELECT url FROM streams")}
    seen, skip = _seen(), set()
    found = []
    for creator in load_creators():
        cfg = raw.get(creator.id, {})
        if not creator.permitted or not cfg.get("vods"):
            continue
        kw = [k.lower() for k in cfg.get("keywords", DEFAULT_KEYWORDS)]
        try:
            entries = _recent(cfg["vods"])
        except Exception as e:
            print(f"  {creator.display_name}: could not list recordings ({str(e)[:120]})")
            continue
        if entries and cfg.get("live") and _is_live(cfg["live"]):
            entries = entries[1:]  # the newest recording is the stream still in progress
        for e in entries:
            url = _clean(e.get("url") or e.get("webpage_url") or "")
            title = e.get("title") or ""
            if not url or url in done or url in seen:
                continue
            minutes = _gta_minutes(url)
            if minutes is not None:  # Twitch: trust the game chapters, not the title
                is_gta = minutes >= MIN_GTA_MINUTES
            else:
                is_gta = any(re.search(rf"\b{re.escape(k)}", title.lower()) for k in kw)
            if not is_gta:
                skip.add(url)  # not GTA: never look at it again
                continue
            found.append((creator, url, title))
            break
    if skip:
        _mark_seen(skip)
    return found
