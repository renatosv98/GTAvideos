"""Agent 1 - Transcriber.

Input:  a downloaded stream (+ chat replay, + yt-dlp info).
Output: data/streams/<id>/report.json with
        - words:   full word-level transcript (absolute seconds)
        - signals: per-second loudness / chat z-scores, YouTube "most replayed" heatmap when available
        - spikes:  the strongest audio/chat peaks (cheap prior for the selector)
Work is cached per 10-minute chunk, so an interrupted run resumes where it stopped.
"""
from __future__ import annotations

import json
import subprocess
from pathlib import Path

import numpy as np

from ..config import SETTINGS, Creator
from ..signals import _z, audio_loudness, chat_activity, find_candidates

LANG = {"spanish": "es", "english": "en", "portuguese": "pt", "french": "fr", "italian": "it", "german": "de"}
SR = 16000


def _decode(video: Path, start: float, dur: float) -> np.ndarray:
    raw = subprocess.run(
        ["ffmpeg", "-v", "error", "-ss", f"{start:.2f}", "-t", f"{dur:.2f}", "-i", str(video),
         "-vn", "-ac", "1", "-ar", str(SR), "-f", "f32le", "-"],
        check=True, capture_output=True,
    ).stdout
    return np.frombuffer(raw, dtype=np.float32)


def _transcribe_array(audio: np.ndarray, language: str | None) -> list[dict]:
    """Word list with times relative to the start of `audio`."""
    cfg = SETTINGS["transcription"]
    words: list[dict] = []
    if cfg.get("engine", "mlx") == "mlx":
        import mlx_whisper

        r = mlx_whisper.transcribe(audio, path_or_hf_repo=cfg.get("mlx_model", "mlx-community/whisper-large-v3-turbo"),
                                   word_timestamps=True, language=language, condition_on_previous_text=False,
                                   verbose=None)
        for seg in r.get("segments", []):
            for w in seg.get("words", []):
                words.append({"w": w["word"].strip(), "s": float(w["start"]), "e": float(w["end"])})
    else:  # faster-whisper on CPU
        from faster_whisper import WhisperModel

        model = WhisperModel(cfg.get("model", "small"), device=cfg.get("device", "auto"),
                             compute_type=cfg.get("compute_type", "int8"))
        segs, _ = model.transcribe(audio, word_timestamps=True, vad_filter=True, language=language)
        for seg in segs:
            for w in seg.words or []:
                words.append({"w": w.word.strip(), "s": float(w.start), "e": float(w.end)})
    return [w for w in words if w["w"]]


def transcribe_stream(video: Path, duration: float, language: str | None, cache_dir: Path) -> list[dict]:
    chunk = int(SETTINGS["transcription"].get("chunk_seconds", 600))
    cache_dir.mkdir(parents=True, exist_ok=True)
    n = int(np.ceil(duration / chunk))
    words: list[dict] = []
    for i in range(n):
        f = cache_dir / f"chunk_{i:04d}.json"
        if f.exists():
            part = json.loads(f.read_text())
        else:
            start = i * chunk
            print(f"    transcribing {start // 3600:.0f}h{start % 3600 // 60:02.0f}m ({i + 1}/{n})")
            audio = _decode(video, start, chunk)
            part = [{"w": w["w"], "s": round(start + w["s"], 2), "e": round(start + w["e"], 2)}
                    for w in (_transcribe_array(audio, language) if audio.size else [])]
            f.write_text(json.dumps(part, ensure_ascii=False))
        words += part
    return words


def _heatmap(info: dict, seconds: int) -> list[float] | None:
    """YouTube 'most replayed' curve resampled per second (0..1), when yt-dlp provides it."""
    hm = info.get("heatmap")
    if not hm:
        return None
    out = np.zeros(seconds, dtype=np.float32)
    for h in hm:
        a, b = int(h["start_time"]), max(int(h["start_time"]) + 1, int(h["end_time"]))
        out[a:min(b, seconds)] = h["value"]
    return [round(float(x), 3) for x in out]


def run(video: Path, chat: Path | None, info: dict, creator: Creator, stream_dir: Path) -> dict:
    report_file = stream_dir / "report.json"
    if report_file.exists():
        print("    report already built (delete report.json to redo)")
        return json.loads(report_file.read_text())

    print("    measuring loudness + chat ...")
    loud = audio_loudness(video)
    seconds = len(loud)
    chat_arr = chat_activity(chat, seconds)
    spikes = find_candidates(loud, chat_arr)

    language = LANG.get(creator.language.lower())
    words = transcribe_stream(video, seconds, language, stream_dir / "transcript")

    loud_z = _z(loud, 5)
    chat_z = _z(chat_arr[:seconds], 10) if chat_arr is not None else None
    report = {
        "title": info.get("title", ""),
        "creator": creator.id,
        "language": creator.language,
        "duration": seconds,
        "words": words,
        "signals": {
            "loudness_z": [round(float(x), 2) for x in loud_z],
            "chat_z": [round(float(x), 2) for x in chat_z] if chat_z is not None else None,
            "most_replayed": _heatmap(info, seconds),
        },
        "spikes": [{"peak": s.peak, "start": s.start, "end": s.end, "score": round(s.score, 2)} for s in spikes],
    }
    report_file.write_text(json.dumps(report, ensure_ascii=False))
    print(f"    {seconds / 3600:.1f}h, {len(words)} words, chat: {'yes' if chat_z is not None else 'no'}, "
          f"most-replayed: {'yes' if report['signals']['most_replayed'] else 'no'}")
    return report


def words_between(words: list[dict], start: float, end: float) -> list[dict]:
    return [w for w in words if start <= w["s"] < end]


def phrases(words: list[dict], pause: float = 0.7) -> list[tuple[float, float, str]]:
    """Group words into phrases split at pauses, as (start, end, text)."""
    out: list[tuple[float, float, str]] = []
    cur: list[dict] = []
    for w in words:
        if cur and w["s"] - cur[-1]["e"] > pause:
            out.append((cur[0]["s"], cur[-1]["e"], " ".join(x["w"] for x in cur)))
            cur = []
        cur.append(w)
    if cur:
        out.append((cur[0]["s"], cur[-1]["e"], " ".join(x["w"] for x in cur)))
    return out
