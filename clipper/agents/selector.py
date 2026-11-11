"""Agent 2 - Clip selector (Claude).

Two passes so a 6-hour stream fits and every pick is grounded in what's on screen:
  scout:  reads the WHOLE stream timeline (transcript + loudness/chat/most-replayed markers) and shortlists moments.
  editor: gets each shortlisted moment's exact phrase timings + still frames and plans the final clips:
          cuts (keep_segments), hook, title, viral_chance, music mood, copyright risks.
Both passes read config/playbook.md (what works on this channel, written by the analyst) and recent feedback
from the producer, the analyst and your review decisions.
"""
from __future__ import annotations

import json
import subprocess
import tempfile
from pathlib import Path
from typing import Any

from .. import db
from ..config import ROOT, SETTINGS, Creator
from ..llm import ask_json
from ..music import MOODS
from ..render import plan_segments
from .transcriber import phrases, words_between

PLAYBOOK = ROOT / "config" / "playbook.md"

RULES = """You are the clip selector of a GTA 6 shorts channel (YouTube Shorts, TikTok, Instagram Reels) that clips
streams from creators who gave permission. Your only goal: shorts that get the most views.

Editor's rules:
- A short is a mini-story: setup (2-5 s) -> escalation -> payoff (fail, win, twist, scream, punchline) -> 1-3 s reaction, then END.
- Reject moments without a clear payoff. Life talk, menus, driving around, eating, reading chat = reject, even if loud.
- Prefer: dramatic fails, last-second wins/losses, rage, chaos, absurd game physics, big reveals, first reactions to GTA 6.
- Start as late as possible: the first second must already be interesting. End right after the reaction.
- Cut dead air: drop silences > 1.5 s, results/loading screens that drag, off-topic chatter, but keep silent moments
  where the action on screen IS the story.
- Ideal length 20-40 s of kept footage.

Reference short (scored 80): IlloJuan's GTA 5 triathlon. Hook text "Iba 2º a 10 metros de la meta...". He sprints for 2nd
yelling "me va a dar un infarto", his character collapses face-first from exhaustion metres from the line, others overtake,
he finishes 5th, rage, then cut straight to the results screen + facepalm: "28 minutos tirados a la basura... qué loco, tío".
Why it works: stakes obvious in 2 s, the visual fail needs no explanation, the reaction is the payoff, dead air cut, ends on a laugh."""

SCOUT_SCHEMA = {
    "type": "object",
    "properties": {"moments": {"type": "array", "items": {
        "type": "object",
        "properties": {
            "start": {"type": "number"}, "end": {"type": "number"},
            "what_happens": {"type": "string"}, "why_it_could_go_viral": {"type": "string"},
        },
        "required": ["start", "end", "what_happens", "why_it_could_go_viral"], "additionalProperties": False}}},
    "required": ["moments"], "additionalProperties": False,
}

EDIT_SCHEMA = {
    "type": "object",
    "properties": {"picks": {"type": "array", "items": {
        "type": "object",
        "properties": {
            "moment_id": {"type": "integer"},
            "keep_segments": {"type": "array", "items": {
                "type": "object", "properties": {"start": {"type": "number"}, "end": {"type": "number"}},
                "required": ["start", "end"], "additionalProperties": False}},
            "hook": {"type": "string"},
            "viral_chance": {"type": "integer"},
            "title": {"type": "string"},
            "caption": {"type": "string"},
            "hashtags": {"type": "array", "items": {"type": "string"}},
            "music_mood": {"type": "string", "enum": MOODS},
            "reason": {"type": "string"},
            "risk_notes": {"type": "string"},
        },
        "required": ["moment_id", "keep_segments", "hook", "viral_chance", "title", "caption", "hashtags",
                     "music_mood", "reason", "risk_notes"],
        "additionalProperties": False}}},
    "required": ["picks"], "additionalProperties": False,
}


def _mmss(t: float) -> str:
    t = int(t)
    return f"{t // 3600}:{t % 3600 // 60:02d}:{t % 60:02d}"


def timeline(report: dict, step: int = 15) -> str:
    """Whole stream as compact lines: one per `step` seconds with signal markers."""
    words, sig = report["words"], report["signals"]
    loud, chat, rep = sig["loudness_z"], sig.get("chat_z"), sig.get("most_replayed")
    lines, wi = [], 0
    for t0 in range(0, report["duration"], step):
        t1 = t0 + step
        txt = []
        while wi < len(words) and words[wi]["s"] < t1:
            txt.append(words[wi]["w"])
            wi += 1
        marks = []
        lmax = max(loud[t0:t1] or [0])
        if lmax >= 1.5:
            marks.append(f"LOUD {lmax:.1f}")
        if chat:
            cmax = max(chat[t0:t1] or [0])
            if cmax >= 1.5:
                marks.append(f"CHAT {cmax:.1f}")
        if rep:
            rmax = max(rep[t0:t1] or [0])
            if rmax >= 0.5:
                marks.append(f"REPLAYED {rmax:.2f}")
        if txt or marks:
            lines.append(f"[{t0}s {_mmss(t0)}]{' {' + ', '.join(marks) + '}' if marks else ''} {' '.join(txt)}")
    return "\n".join(lines)


def _feedback(limit: int = 40) -> str:
    with db.connect() as conn:
        rows = conn.execute("SELECT from_agent, message, created_at FROM agent_feedback WHERE to_agent='selector' "
                            "ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
    return "\n".join(f"- [{r['created_at'][:10]} from {r['from_agent']}] {r['message']}" for r in rows) or "(none yet)"


def _context(creator: Creator) -> str:
    playbook = PLAYBOOK.read_text() if PLAYBOOK.exists() else "(empty)"
    return (f"# Channel playbook (learned from published shorts - follow it)\n{playbook}\n\n"
            f"# Recent feedback for you\n{_feedback()}\n\n"
            f"Creator: {creator.display_name}. Stream language: {creator.language}.")


def _frames(video: Path, start: float, end: float, n: int, out_dir: Path, tag: str) -> list[Path]:
    paths = []
    for k in range(n):
        t = start + (end - start) * (k + 1) / (n + 1)
        p = out_dir / f"{tag}_{k}.jpg"
        subprocess.run(["ffmpeg", "-v", "error", "-y", "-ss", f"{t:.2f}", "-i", str(video), "-frames:v", "1",
                        "-vf", "scale=640:-2", "-q:v", "5", str(p)], check=True)
        paths.append(p)
    return paths


def scout(report: dict, creator: Creator) -> list[dict]:
    cfg = SETTINGS["analysis"]
    spikes = ", ".join(f"{s['peak']:.0f}s" for s in report["spikes"])
    prompt = (f"{_context(creator)}\n\nStream title: \"{report['title']}\"\n"
              f"Strongest audio/chat spikes (seconds): {spikes}\n\n"
              f"Below is the full stream timeline: one line per 15 s, with markers where loudness/chat/replays spike.\n"
              f"Shortlist up to {cfg['shortlist']} moments that could become great shorts. Give each a window "
              f"(start/end seconds, 30-120 s wide) that contains the full setup and payoff. "
              f"Don't only follow the markers: quiet stories and reveals can be great too.\n\n{timeline(report)}")
    return ask_json(RULES, prompt, SCOUT_SCHEMA)["moments"][: cfg["shortlist"]]


def edit(video: Path, report: dict, creator: Creator, moments: list[dict]) -> list[dict[str, Any]]:
    cfg = SETTINGS["analysis"]
    n_frames = int(cfg.get("frames_per_candidate", 3))
    parts, images = [], []
    with tempfile.TemporaryDirectory(prefix="clipper-frames-") as tmp:
        for i, m in enumerate(moments):
            ws = words_between(report["words"], m["start"], m["end"])
            lines = "\n".join(f"  [{a:.1f}-{b:.1f}] {t}" for a, b, t in phrases(ws)) or "  (no speech)"
            fr = _frames(video, m["start"], m["end"], n_frames, Path(tmp), f"m{i}")
            names = ", ".join(f"{p.name} @ {m['start'] + (m['end'] - m['start']) * (k + 1) / (n_frames + 1):.0f}s"
                              for k, p in enumerate(fr))
            images += fr
            parts.append(f"## Moment {i}: {m['start']:.0f}-{m['end']:.0f}s\nScout's note: {m['what_happens']} "
                         f"({m['why_it_could_go_viral']})\nFrames: {names}\nPhrases (seconds):\n{lines}")
        prompt = (
            f"{_context(creator)}\n\n"
            f"Plan the final shorts from these shortlisted moments. For each pick:\n"
            f"- keep_segments: exact cut list (absolute seconds, in order) within the moment, {cfg['min_seconds']}-"
            f"{cfg['max_seconds']} s in total; cut at phrase boundaries.\n"
            f"- hook: 3-8 words on screen for the first 3 s, curiosity without spoiling the payoff.\n"
            f"- viral_chance 0-100, calibrated: 70-90 = complete story + visual payoff anyone gets without context + big "
            f"reaction; 40-69 = clear payoff but slower or needs context; 15-39 = funny line, little action; <15 = skip it.\n"
            f"- music_mood: background music that fits ({', '.join(MOODS)}); it plays quietly under the voice.\n"
            f"- title (max 70 chars, no clickbait lies), caption, 3-6 hashtags, written in {creator.language}; "
            f"keep 'GTA 6' / '#GTA6' as is.\n"
            f"- risk_notes: copyright/policy risks you can see or hear (in-game radio or licensed music, other people's "
            f"videos on screen, slurs, personal info), or \"none\".\n"
            f"Skip moments that don't work. Return up to {cfg['max_clips']} picks, highest viral_chance first.\n\n"
            + "\n\n".join(parts))
        picks = ask_json(RULES, prompt, EDIT_SCHEMA, images=images)["picks"]

    cleaned = []
    for p in picks:
        if not 0 <= p["moment_id"] < len(moments) or p["viral_chance"] < 15:
            continue
        m = moments[p["moment_id"]]
        segs = plan_segments(m["start"], m["end"], [(k["start"], k["end"]) for k in p["keep_segments"]])
        total = sum(b - a for a, b in segs)
        if total < cfg["min_seconds"] or total > cfg["max_seconds"] + 5:
            print(f"    skipping '{p['title']}': {total:.0f}s after cuts")
            continue
        p.update(start=segs[0][0], end=segs[-1][1], cuts=[list(s) for s in segs],
                 words=words_between(report["words"], segs[0][0], segs[-1][1]))
        cleaned.append(p)
    cleaned.sort(key=lambda p: p["viral_chance"], reverse=True)
    return cleaned[: cfg["max_clips"]]


def run(video: Path, report: dict, creator: Creator, stream_dir: Path, redo: bool = False) -> list[dict]:
    picks_file = stream_dir / "picks.json"
    if picks_file.exists() and not redo:
        print("    picks already made (use --reselect to redo)")
        return json.loads(picks_file.read_text())
    print("    scouting the full stream ...")
    moments = scout(report, creator)
    (stream_dir / "shortlist.json").write_text(json.dumps(moments, ensure_ascii=False, indent=1))
    print(f"    {len(moments)} moments shortlisted; planning the edits ...")
    picks = edit(video, report, creator, moments)
    picks_file.write_text(json.dumps(picks, ensure_ascii=False, indent=1))
    return picks
