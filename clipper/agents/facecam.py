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


# ---- token saver (27 Nov): reuse a nearby clip's camera box instead of asking Claude for every clip ----
# Within one stream, clips <=45 min apart had the same box 97% of the time (169 of 175 pairs).
REUSE_WINDOW_S = 2700
MATCH_MIN = 0.80
CONTROL_GAP = 0.10   # camera box must match clearly better than a gameplay area


def _region_hist(video: Path, t: float, box: dict):
    """Colour histogram of the camera box at time t (the streamer's room looks the same while the camera is there)."""
    import numpy as np
    w, h = 64, 64
    crop = f"crop=iw*{box['w']}:ih*{box['h']}:iw*{box['x']}:ih*{box['y']},scale={w}:{h}"
    raw = subprocess.run(["ffmpeg", "-v", "error", "-ss", f"{t:.2f}", "-i", str(video), "-frames:v", "1", "-vf", crop,
                          "-f", "rawvideo", "-pix_fmt", "rgb24", "-"], capture_output=True, check=True).stdout
    px = np.frombuffer(raw, dtype=np.uint8).reshape(-1, 3)
    hist = np.concatenate([np.histogram(px[:, i], bins=16, range=(0, 256))[0] for i in range(3)]).astype(float)
    return hist / max(hist.sum(), 1)


def same_camera(video: Path, t_ref: float, t_new: float, box: dict) -> float:
    """Similarity 0-1 of the box region at two times (histogram correlation)."""
    import numpy as np
    a, b = _region_hist(video, t_ref, box), _region_hist(video, t_new, box)
    if a.std() == 0 or b.std() == 0:
        return 0.0
    return float(np.corrcoef(a, b)[0, 1])


def reuse_nearby(conn, clip_id: int, video: Path, start: float, end: float):
    """Camera box of the nearest clip of the same stream (<=45 min away) if the pixels confirm it; else None."""
    import json
    row = conn.execute("SELECT stream_id FROM clips WHERE id=?", (clip_id,)).fetchone()
    if not row:
        return None
    best = None
    for r in conn.execute("SELECT id, start, end, edit_json FROM clips WHERE stream_id=? AND id!=? AND edit_json LIKE "
                          "'%facecam%'", (row[0], clip_id)):
        fc = json.loads(r[3] or "{}").get("facecam")
        if fc and abs(r[1] - start) <= REUSE_WINDOW_S and (best is None or abs(r[1] - start) < abs(best[0] - start)):
            best = (r[1], r[2], fc, r[0])
    if not best:
        return None
    t_ref, t_new = (best[0] + best[1]) / 2, (start + end) / 2
    try:
        sim = same_camera(video, t_ref, t_new, best[2])
        # control: an area of gameplay of the same size. If it is just as similar (same scene), the check proves
        # nothing, so ask Claude instead.
        b = best[2]
        cx = 0.5 - b["w"] / 2 if b["x"] + b["w"] / 2 < 0.5 else 0.25 - b["w"] / 2
        ctrl = same_camera(video, t_ref, t_new, {**b, "x": max(0.0, cx), "y": max(0.0, 0.5 - b["h"] / 2)})
    except Exception:
        return None
    return (b, best[3], sim) if sim >= MATCH_MIN and sim - ctrl >= CONTROL_GAP else None
