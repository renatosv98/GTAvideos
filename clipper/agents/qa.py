"""Quality check of a finished short (Claude vision), run by the producer before a clip can be approved.

Checks what broke before: the top panel must show the streamer (webcam or their avatar), captions readable,
the story must make sense, it must not repeat a clip we already have, and the text must be spelled correctly.
"""
from __future__ import annotations

import subprocess
import tempfile
from pathlib import Path

from .. import llm

SYSTEM = ("You are the quality checker of a GTA shorts channel. You see frames of a finished vertical short "
          "(top panel = streamer's face cam or avatar, bottom = gameplay/screen, captions in the middle, hook text "
          "at the start). Be strict about real defects, not about taste.")

SCHEMA = {
    "type": "object",
    "properties": {
        "passed": {"type": "boolean"},
        "issues": {"type": "array", "items": {"type": "string"}},
        "duplicate_of": {"type": "string"},
        "title": {"type": "string"},
        "caption": {"type": "string"},
    },
    "required": ["passed", "issues", "duplicate_of", "title", "caption"], "additionalProperties": False,
}


def _frames(short: Path, tmp: Path) -> list[Path]:
    dur = float(subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0",
                                str(short)], capture_output=True, text=True, check=True).stdout.strip())
    out = []
    for i, p in enumerate((0.08, 0.35, 0.62, 0.9)):
        f = tmp / f"qa{i}.jpg"
        subprocess.run(["ffmpeg", "-v", "error", "-y", "-ss", f"{dur * p:.2f}", "-i", str(short), "-frames:v", "1",
                        "-vf", "scale=540:-2", str(f)], check=True)
        out.append(f)
    return out


def review(short: Path, clip: dict, transcript: str, existing_titles: list[str], language: str) -> dict:
    with tempfile.TemporaryDirectory() as d:
        frames = _frames(Path(short), Path(d))
        existing = "\n".join(f"- {t}" for t in existing_titles) or "(none)"
        return llm.ask_json(SYSTEM, (
            f"Check this short ({language}). Frames are at 8%, 35%, 62% and 90% of the video.\n\n"
            f"Title: {clip['title']}\nCaption: {clip['caption']}\nHook (shown first 3.5 s): {clip.get('hook')}\n"
            f"What the speaker says: {transcript[:2500]}\n\n"
            f"Clips we already published or scheduled:\n{existing}\n\n"
            "FAIL it (passed=false) only for real defects:\n"
            "1. The top panel does not show the streamer's face or their avatar (e.g. shows a menu, webpage, "
            "game screen, black/empty area) in most frames.\n"
            "2. Captions are unreadable, cut off, or cover the important action.\n"
            "3. The moment makes no sense on its own (no setup/payoff) or is not about GTA.\n"
            "4. It covers the SAME moment/topic as one of the existing clips (set duplicate_of to that title).\n"
            "List every problem in issues (empty if none). Also return title and caption with spelling, accents "
            "and grammar fixed, same language, same meaning and length (unchanged if already correct)."),
            SCHEMA, images=frames, effort="low")
