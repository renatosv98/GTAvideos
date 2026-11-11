"""Claude picks the best moments from the candidates and writes titles/captions + copyright risk notes."""
from __future__ import annotations

import base64
import json
import subprocess
from pathlib import Path
from typing import Any

import anthropic

from .config import SETTINGS, Creator
from .render import plan_segments
from .signals import Candidate
from .transcribe import transcribe_window, words_to_text

SYSTEM = """You are the editor of a GTA 6 shorts channel that clips streams from creators who have given permission.
Your only goal: shorts people watch to the end, rewatch and share. You receive candidate moments from one stream
(found by audio and chat spikes), each with a timestamped transcript and still frames.

Editor's rules:
- A short is a mini-story: setup (2-5 s) -> escalation -> payoff (fail, win, twist, scream, punchline) -> 1-3 s reaction, then END.
- Skip candidates without a clear payoff. Talking about life, menus, driving around, eating, reading chat = reject, even if loud.
- Prefer: dramatic fails, last-second wins/losses, rage, chaos, absurd game physics, big reveals, first reactions to GTA 6.
- Start as late as possible: the first second must already be interesting. End right after the reaction; never trail off.
- Cut dead air: keep_segments lists the parts to KEEP (absolute seconds, in order). Drop silences > 1.5 s, results/loading
  screens that drag, and off-topic chatter, but keep silent moments where the action on screen IS the story.
- hook: 3-8 words burned on screen for the first 3 seconds that create curiosity without spoiling the payoff
  (e.g. "Iba 2º a 10 metros de la meta..."). Same language as the stream.
- Ideal length 20-40 s of kept footage.

Find EVERY candidate that works as a short (several per stream is normal), then rank them.
viral_chance = your honest estimate (0-100) that the short beats this channel's typical views several times over:
- 70-90: complete story with a visual payoff anyone gets without sound or context, strong emotional reaction, rewatchable.
- 40-69: clear payoff and reaction, but slower setup or needs some context.
- 15-39: funny line or moment, little visual action.
- <15: don't pick it.
Be calibrated: most picks land in 20-60; reserve 70+ for moments you'd bet on.

Reference short (scored 80): IlloJuan's GTA 5 triathlon. Hook text "Iba 2º a 10 metros de la meta...". He sprints for 2nd
yelling "me va a dar un infarto", his character collapses face-first from exhaustion metres from the line, others overtake,
he finishes 5th, rage, then cut straight to the results screen + facepalm: "28 minutos tirados a la basura... qué loco, tío".
Why it works: stakes are obvious in 2 s, the visual fail needs no explanation, the reaction is the payoff, dead air cut, ends on a laugh.

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
                    "keep_segments": {"type": "array", "items": {
                        "type": "object", "properties": {"start": {"type": "number"}, "end": {"type": "number"}},
                        "required": ["start", "end"], "additionalProperties": False}},
                    "hook": {"type": "string"},
                    "viral_chance": {"type": "integer", "description": "estimated % chance (0-100) of going viral"},
                    "title": {"type": "string"},
                    "caption": {"type": "string"},
                    "hashtags": {"type": "array", "items": {"type": "string"}},
                    "reason": {"type": "string"},
                    "risk_notes": {"type": "string"},
                },
                "required": ["candidate_id", "start", "end", "keep_segments", "hook", "viral_chance", "title", "caption", "hashtags", "reason", "risk_notes"],
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
                f"Write title, caption and hashtags in {creator.language} (the language of the stream); "
                f"keep 'GTA 6' / '#GTA6' as is.\n"
                f"Return up to {cfg['max_clips']} picks, highest viral_chance first, each {cfg['min_seconds']}-{cfg['max_seconds']} seconds long.",
    }]
    words_by_id: dict[int, list[dict]] = {}
    cache_file = Path(video).parent / "transcripts.json"
    cache: dict[str, list[dict]] = json.loads(cache_file.read_text()) if cache_file.exists() else {}
    for c in candidates:
        key = f"{c.start:.1f}-{c.end:.1f}"
        if key in cache:
            words = cache[key]
        else:
            print(f"  transcribing candidate {c.idx} ({c.start:.0f}s-{c.end:.0f}s)")
            words = cache[key] = transcribe_window(video, c.start, c.end)
            cache_file.write_text(json.dumps(cache))
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
        segs = plan_segments(start, end, [(k["start"], k["end"]) for k in p.get("keep_segments") or []])
        if sum(b - a for a, b in segs) < cfg["min_seconds"]:
            segs = [(start, end)]  # cuts too aggressive: keep the whole range
        p.update(start=segs[0][0], end=segs[-1][1], cuts=[list(x) for x in segs],
                 words=[w for w in words_by_id[c.idx] if start <= w["s"] < end])
        cleaned.append(p)
    cleaned.sort(key=lambda p: p["viral_chance"], reverse=True)  # most likely to go viral first
    return cleaned[: cfg["max_clips"]]
