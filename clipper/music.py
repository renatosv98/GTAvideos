"""Background music library (config/music.yaml -> assets/music/)."""
from __future__ import annotations

import random
import urllib.parse
from pathlib import Path

import requests
import yaml

from .config import ROOT

MOODS = ["funny", "sneaky", "suspense", "awkward", "chaos", "epic", "fail", "chill", "spooky", "victory"]
CATALOG = ROOT / "config" / "music.yaml"
MUSIC_DIR = ROOT / "assets" / "music"


def _catalog() -> dict:
    return yaml.safe_load(CATALOG.read_text())


def track_file(title: str) -> Path:
    return MUSIC_DIR / f"{title}.mp3"


def tracks(mood: str | None = None) -> list[dict]:
    cat = _catalog()
    out = []
    for t in cat["tracks"]:
        if mood and mood not in t["moods"]:
            continue
        if track_file(t["title"]).exists():
            out.append({**t, "artist": t.get("artist", cat["artist"]), "license": t.get("license", cat["license"])})
    return out


def pick(mood: str, avoid: list[str] | None = None) -> dict | None:
    """A downloaded track for this mood, avoiding recently used titles when possible."""
    options = tracks(mood) or tracks("funny") or tracks()
    fresh = [t for t in options if t["title"] not in (avoid or [])]
    return random.choice(fresh or options) if options else None


def credit(track: dict) -> str:
    return f'Music: "{track["title"]}" {track["artist"]} (incompetech.com). Licensed under {track["license"]}'


def download() -> int:
    cat = _catalog()
    MUSIC_DIR.mkdir(parents=True, exist_ok=True)
    ok, missing = 0, []
    for t in cat["tracks"]:
        f = track_file(t["title"])
        if f.exists() and f.stat().st_size > 50_000:
            ok += 1
            continue
        url = cat["source"].format(title=urllib.parse.quote(t["title"]))
        r = requests.get(url, timeout=60)
        if r.status_code == 200 and len(r.content) > 50_000 and (r.content[:3] == b"ID3" or r.content[0] == 0xFF):
            f.write_bytes(r.content)
            ok += 1
            print(f"  ✅ {t['title']}")
        else:
            missing.append(t["title"])
            print(f"  ❌ {t['title']} (HTTP {r.status_code})")
    print(f"\n{ok} tracks ready in {MUSIC_DIR.relative_to(ROOT)}"
          + (f"; not found: {', '.join(missing)} (fix the title in config/music.yaml or remove it)" if missing else ""))
    return 0
