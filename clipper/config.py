from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")

# Homebrew's bin dirs aren't on PATH in every shell/launchd context; ffmpeg lives there.
for _p in ("/opt/homebrew/bin", "/usr/local/bin"):
    if Path(_p).is_dir() and _p not in os.environ.get("PATH", "").split(os.pathsep):
        os.environ["PATH"] = _p + os.pathsep + os.environ.get("PATH", "")


def _load(name: str) -> dict[str, Any]:
    with open(ROOT / "config" / name, encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


SETTINGS: dict[str, Any] = _load("settings.yaml")
DATA = ROOT / SETTINGS.get("data_dir", "data")
SECRETS = ROOT / "secrets"
DATA.mkdir(parents=True, exist_ok=True)
SECRETS.mkdir(parents=True, exist_ok=True)


@dataclass
class Creator:
    id: str
    display_name: str
    credit: str
    match: list[str]
    permission: dict[str, Any]
    links: dict[str, Any]
    language: str = "English"
    facecam: dict[str, float] | None = None  # x, y, w, h as fractions of the source frame

    @property
    def permitted(self) -> bool:
        p = self.permission or {}
        return bool(p.get("granted")) and bool(p.get("source"))


def load_creators() -> list[Creator]:
    raw = _load("creators.yaml").get("creators", []) or []
    return [
        Creator(
            id=c["id"],
            display_name=c.get("display_name", c["id"]),
            credit=c.get("credit", c.get("display_name", c["id"])),
            match=[m.lower() for m in c.get("match", [])],
            permission=c.get("permission", {}) or {},
            links=c.get("links", {}) or {},
            language=c.get("language", "English"),
            facecam=c.get("facecam"),
        )
        for c in raw
    ]


class PermissionError_(Exception):
    pass


def resolve_creator(info: dict[str, Any], forced_id: str | None = None) -> Creator:
    """Match a yt-dlp info dict to an allowlisted creator who has granted permission."""
    creators = load_creators()
    if forced_id:
        found = [c for c in creators if c.id == forced_id]
    else:
        haystack = " ".join(
            str(info.get(k, "") or "")
            for k in ("channel_url", "uploader_url", "channel_id", "uploader_id", "channel", "uploader", "webpage_url")
        ).lower()
        found = [c for c in creators if any(m and m in haystack for m in c.match)]
    if not found:
        raise PermissionError_(
            f"Channel '{info.get('channel') or info.get('uploader')}' is not in config/creators.yaml. "
            "Add it with permission evidence before clipping."
        )
    c = found[0]
    if not c.permitted:
        raise PermissionError_(
            f"Creator '{c.id}' is listed but permission.granted is not true (or permission.source is empty). "
            "Record how they gave permission first."
        )
    return c
