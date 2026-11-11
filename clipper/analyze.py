"""Claude picks the best moments from the candidates and writes titles/captions + copyright risk notes."""
from __future__ import annotations

import base64
import json
import subprocess
from pathlib import Path
from typing import Any

import anthropic

from .config import SETTINGS, Creator
from .signals import Candidate
from .transcribe import transcribe_window, words_to_text

SYSTEM = """You are the editor of a GTA 6 shorts channel that clips streams from creators who have given permission.
You receive candidate moments from one stream (found by audio and chat spikes), each with a timestamped transcript and sometimes still frames.
Pick the moments most likely to perform as 15-59 second vertical shorts: a clear hook in the first 2 seconds, a payoff, and sense without context.
Prefer funny reactions, chaos, surprising game mechanics, first impressions of new GTA 6 features, and strong emotional moments.
Skip slow menus, loading screens, dead air, and moments that only make sense with prior context.

For each pick, choose exact start/end seconds (absolute stream time, inside the candidate window) so the clip starts right before the hook and ends right after the payoff.
Write a punchy title (max 70 chars, no clickbait lies), a short caption, and 3-6 hashtags.
In risk_notes, flag anything that could cause a copyright claim or policy problem: in-game radio or licensed music audible, background music on stream, other people's content on screen (videos, TikToks, other streamers), slurs, personal info, or anything else. Write "none" if clean."""

PICK_SCHEMA = {
    "type": "object",
    "properties": {
        "picks": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "candidate_id": {"type": "integer"},
                    "start": {"type": "number"},
                    "end": {"type": "number"},
                    "virality": {"type": "integer", "description": "1-10"},
                    "title": {"type": "string"},
                    "caption": {"type": "string"},
                    "hashtags": {"type": "array", "items": {"type": "string"}},
                    "reason": {"type": "string"},
                    "risk_notes": {"type": "string"},
                },
                "required": ["candidate_id", "start", "end", "virality", "title", "caption", "hashtags", "reason", "risk_notes"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["picks"],
    "additionalProperties": False,
}


def _frame_b64(video: Path, t: float) -> str:
    out = subprocess.run(
        ["ffmpeg", "-v", "error", "-ss", f"{t:.2f}", "-i", str(video), "-frames:v", "1",
         "-vf", "scale=640:-2", "-q:v", "5", "-f", "image2", "-c:v", "mjpeg", "-"],
        check=True, capture_output=True,
    ).stdout
    return base64.b64encode(out).decode()


def analyze(video: Path, title: str, creator: Creator, candidates: list[Candidate]) -> list[dict[str, Any]]:
    cfg = SETTINGS["analysis"]
    n_frames = cfg.get("frames_per_candidate", 0)

    content: list[dict[str, Any]] = [{
        "type": "text",
        "text": f"Stream: \"{title}\" by {creator.display_name}.\n"
                f"Return up to {cfg['max_clips']} picks, best first, each {cfg['min_seconds']}-{cfg['max_seconds']} seconds long.",
    }]
    words_by_id: dict[int, list[dict]] = {}
    for c in candidates:
        print(f"  transcribing candidate {c.idx} ({c.start:.0f}s-{c.end:.0f}s)")
        words = transcribe_window(video, c.start, c.end)
        words_by_id[c.idx] = words
        content.append({
            "type": "text",
            "text": f"\n## Candidate {c.idx}: window {c.start:.0f}s-{c.end:.0f}s, spike at {c.peak:.0f}s, signal score {c.score:.2f}\n"
                    f"{words_to_text(words)}",
        })
        for k in range(n_frames):
            t = c.start + (c.end - c.start) * (k + 1) / (n_frames + 1)
            content.append({"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": _frame_b64(video, t)}})

    client = anthropic.Anthropic()
    with client.beta.messages.stream(
        model=cfg["model"],
        max_tokens=32000,
        system=SYSTEM,
        messages=[{"role": "user", "content": content}],
        thinking={"type": "adaptive"},
        output_config={"effort": cfg.get("effort", "medium"), "format": {"type": "json_schema", "schema": PICK_SCHEMA}},
        betas=["server-side-fallback-2026-07-01"],
        fallbacks="default",
    ) as stream:
        msg = stream.get_final_message()

    if msg.stop_reason == "refusal":
        raise RuntimeError(f"Claude declined this stream: {getattr(msg, 'stop_details', None)}")
    text = next((b.text for b in msg.content if b.type == "text"), "")
    picks = json.loads(text)["picks"]

    by_id = {c.idx: c for c in candidates}
    cleaned = []
    for p in picks:
        c = by_id.get(p["candidate_id"])
        if not c:
            continue
        start = max(c.start, min(p["start"], c.end - cfg["min_seconds"]))
        end = min(c.end, max(p["end"], start + cfg["min_seconds"]), start + cfg["max_seconds"])
        p.update(start=start, end=end, words=[w for w in words_by_id[c.idx] if start <= w["s"] < end])
        cleaned.append(p)
    return cleaned[: cfg["max_clips"]]
