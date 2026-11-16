"""Find the streamer's webcam in a clip (Claude vision), because streamers move it between scenes and streams.

Returns {x, y, w, h} as fractions of the frame, or {} when there is no real camera (avatar, BRB screen...),
in which case the producer uses the layout without a face panel.
"""
from __future__ import annotations

import subprocess
import tempfile
from pathlib import Path

from .guidance import brief
from .. import llm

SYSTEM = ("You locate the streamer's live webcam overlay in screenshots of a livestream. "
          "Be precise: use the yellow grid (every line = 10% of width/height) to measure the edges.")

SCHEMA = {
    "type": "object",
    "properties": {
        "has_camera": {"type": "boolean"},
        "x": {"type": "number"}, "y": {"type": "number"}, "w": {"type": "number"}, "h": {"type": "number"},
        "note": {"type": "string"},
    },
    "required": ["has_camera", "x", "y", "w", "h", "note"], "additionalProperties": False,
}


def _frames(video: Path, times: list[float], tmp: Path) -> list[Path]:
    out = []
    for i, t in enumerate(times):
        f = tmp / f"frame{i}.jpg"
        subprocess.run(["ffmpeg", "-v", "error", "-y", "-ss", f"{t:.2f}", "-i", str(video), "-frames:v", "1",
                        "-vf", "scale=1280:-2,drawgrid=w=iw/10:h=ih/10:t=1:c=yellow@0.6", str(f)], check=True)
        out.append(f)
    return out


def detect(video: Path, start: float, end: float) -> dict:
    span = end - start
    times = [start + span * p for p in (0.15, 0.5, 0.85)]
    with tempfile.TemporaryDirectory() as d:
        frames = _frames(Path(video), times, Path(d))
        r = llm.ask_json(SYSTEM + brief("producer"), (
            "These are 3 screenshots from the same short clip of a livestream.\n"
            "Find the STREAMER'S FACE ON STREAM: their live webcam (a real person's camera feed, usually a rectangle "
            "in a corner) OR the animated avatar they use instead of a webcam (e.g. a talking cartoon character, "
            "PNGtuber/VTuber, which moves with their voice). If both are visible, prefer the real webcam.\n"
            "Does NOT count: a photo or person inside a webpage, video or game being watched; logos; emotes.\n"
            "If the face/avatar is visible in at least 2 of the 3 screenshots, return its rectangle as fractions of the "
            "full frame (x, y = top-left corner; w, h = size; 0-1), tight around the camera feed itself (exclude "
            "chat, alerts and borders). Otherwise has_camera=false and zeros."), SCHEMA, images=frames,
            effort="low")
    if not r["has_camera"] or r["w"] < 0.05 or r["h"] < 0.05:
        return {}
    x, y = max(0.0, r["x"]), max(0.0, r["y"])
    return {"x": round(x, 3), "y": round(y, 3),
            "w": round(min(r["w"], 1 - x), 3), "h": round(min(r["h"], 1 - y), 3)}
