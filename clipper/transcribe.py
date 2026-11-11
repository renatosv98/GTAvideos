from __future__ import annotations

import subprocess
import tempfile
from functools import lru_cache
from pathlib import Path

from .config import SETTINGS


@lru_cache(maxsize=1)
def _model():
    from faster_whisper import WhisperModel

    cfg = SETTINGS["transcription"]
    return WhisperModel(cfg["model"], device=cfg["device"], compute_type=cfg["compute_type"])


def transcribe_window(video: Path, start: float, end: float) -> list[dict]:
    """Word-level transcript of [start, end]. Timestamps are absolute (seconds into the stream)."""
    with tempfile.TemporaryDirectory() as tmp:
        wav = Path(tmp) / "a.wav"
        subprocess.run(
            ["ffmpeg", "-v", "error", "-y", "-ss", f"{start:.2f}", "-t", f"{end - start:.2f}",
             "-i", str(video), "-vn", "-ac", "1", "-ar", "16000", str(wav)],
            check=True,
        )
        segments, _ = _model().transcribe(
            str(wav), word_timestamps=True, vad_filter=True,
            language=SETTINGS["transcription"].get("language"),
        )
        words = []
        for seg in segments:
            for w in seg.words or []:
                words.append({"w": w.word.strip(), "s": round(start + w.start, 2), "e": round(start + w.end, 2)})
        return words


def words_to_text(words: list[dict]) -> str:
    """Compact timestamped transcript: one line per ~5s."""
    lines, cur, t0 = [], [], None
    for w in words:
        if t0 is None:
            t0 = w["s"]
        cur.append(w["w"])
        if w["e"] - t0 >= 5:
            lines.append(f"[{t0:.0f}s] {' '.join(cur)}")
            cur, t0 = [], None
    if cur:
        lines.append(f"[{t0:.0f}s] {' '.join(cur)}")
    return "\n".join(lines) or "(no speech detected)"
