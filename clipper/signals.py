"""Cheap signals that find candidate moments in hours of footage without an LLM.

- audio: per-second loudness (screams, laughter, explosions)
- chat:  per-second message rate + hype-word density from the live-chat replay
"""
from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass
from pathlib import Path

import numpy as np

from .config import SETTINGS

SR = 8000  # audio sample rate used for loudness only


def audio_loudness(video: Path) -> np.ndarray:
    """Per-second RMS loudness in dB, streamed from ffmpeg so memory stays flat."""
    proc = subprocess.Popen(
        ["ffmpeg", "-v", "error", "-i", str(video), "-vn", "-ac", "1", "-ar", str(SR), "-f", "s16le", "-"],
        stdout=subprocess.PIPE,
    )
    out: list[float] = []
    chunk = SR * 2  # one second of int16
    assert proc.stdout
    while True:
        buf = proc.stdout.read(chunk)
        if not buf:
            break
        a = np.frombuffer(buf, dtype=np.int16).astype(np.float32)
        rms = np.sqrt(np.mean(a * a)) if a.size else 0.0
        out.append(20 * np.log10(rms + 1e-6))
    proc.wait()
    return np.array(out, dtype=np.float32)


def _chat_text(action: dict) -> str | None:
    item = action.get("addChatItemAction", {}).get("item", {})
    r = item.get("liveChatTextMessageRenderer") or item.get("liveChatPaidMessageRenderer")
    if not r:
        return None
    return "".join(run.get("text", "") or run.get("emoji", {}).get("shortcuts", [""])[0] for run in r.get("message", {}).get("runs", []))


def chat_activity(chat_file: Path | None, seconds: int) -> np.ndarray | None:
    """Per-second chat score: messages + 2x hype-word hits."""
    if not chat_file or not chat_file.exists():
        return None
    hype = [w.lower() for w in SETTINGS["highlights"]["hype_words"]]
    score = np.zeros(seconds + 1, dtype=np.float32)
    with open(chat_file, encoding="utf-8") as f:
        for line in f:
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue
            replay = obj.get("replayChatItemAction", {})
            t = int(replay.get("videoOffsetTimeMsec", -1)) // 1000
            if not 0 <= t <= seconds:
                continue
            for act in replay.get("actions", []):
                text = _chat_text(act)
                if text is None:
                    continue
                low = f" {text.lower()} "
                score[t] += 1 + 2 * sum(1 for w in hype if f" {w} " in low or (len(w) > 2 and w in low))
    return score if score.sum() > 0 else None


def _z(x: np.ndarray, smooth: int) -> np.ndarray:
    if smooth > 1:
        x = np.convolve(x, np.ones(smooth) / smooth, mode="same")
    sd = x.std()
    return (x - x.mean()) / sd if sd > 0 else x * 0


@dataclass
class Candidate:
    idx: int
    peak: float
    start: float
    end: float
    score: float


def find_candidates(loudness: np.ndarray, chat: np.ndarray | None) -> list[Candidate]:
    cfg = SETTINGS["highlights"]
    n = len(loudness)
    combined = _z(loudness, 5)
    if chat is not None:
        # chat lags the moment by a few seconds; shift it back
        c = np.roll(_z(chat[:n] if len(chat) >= n else np.pad(chat, (0, n - len(chat))), 10), -6)
        w = cfg["chat_weight"]
        combined = w * c + (1 - w) * combined

    order = np.argsort(combined)[::-1]
    picked: list[int] = []
    gap = cfg["min_gap_seconds"]
    for i in order:
        if len(picked) >= cfg["candidates"]:
            break
        if i < 20 or i > n - 10:  # skip intro/outro edges
            continue
        if all(abs(int(i) - p) >= gap for p in picked):
            picked.append(int(i))

    return [
        Candidate(
            idx=k,
            peak=float(p),
            start=float(max(0, p - cfg["window_before"])),
            end=float(min(n, p + cfg["window_after"])),
            score=float(combined[p]),
        )
        for k, p in enumerate(sorted(picked))
    ]
