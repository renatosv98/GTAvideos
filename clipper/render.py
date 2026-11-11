"""Render a vertical 1080x1920 short with ffmpeg.

stack: gameplay on top, your own footage on the bottom, captions on the seam, creator credit.
blur:  full gameplay centred over a blurred copy of itself, captions below, creator credit.
"""
from __future__ import annotations

import random
import subprocess
from pathlib import Path

from .config import ROOT, SETTINGS, Creator

FONT_CANDIDATES = [
    ROOT / "assets/fonts/font.ttf",
    Path("/System/Library/Fonts/Supplemental/Arial Black.ttf"),
    Path("/System/Library/Fonts/Supplemental/Arial Bold.ttf"),
    Path("/Library/Fonts/Arial.ttf"),
    Path("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"),
]


def render_ffmpeg() -> str:
    """ffmpeg build with libass (captions) + freetype (text). Homebrew's default build has neither."""
    try:
        import imageio_ffmpeg

        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:
        return "ffmpeg"


def _font() -> Path | None:
    return next((p for p in FONT_CANDIDATES if p.exists()), None)


def _duration(path: Path) -> float:
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "default=nw=1:nk=1", str(path)],
        capture_output=True, text=True, check=True,
    ).stdout.strip()
    return float(out or 0)


def _ts(t: float) -> str:
    t = max(0.0, t)
    h, rem = divmod(t, 3600)
    m, s = divmod(rem, 60)
    return f"{int(h)}:{int(m):02d}:{s:05.2f}"


def write_ass(words: list[dict], clip_start: float, out: Path, y: int) -> None:
    cfg = SETTINGS["render"]
    per = cfg.get("caption_words_per_line", 3)
    header = f"""[Script Info]
ScriptType: v4.00+
PlayResX: {cfg['width']}
PlayResY: {cfg['height']}
WrapStyle: 0

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Cap,{cfg.get('font', 'Arial Black')},84,&H00FFFFFF,&H0000FFFF,&H00000000,&H64000000,-1,0,0,0,100,100,0,0,1,7,3,5,60,60,0,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""
    lines = []
    for i in range(0, len(words), per):
        group = words[i:i + per]
        s = group[0]["s"] - clip_start
        e = group[-1]["e"] - clip_start
        nxt = words[i + per]["s"] - clip_start if i + per < len(words) else e + 0.4
        e = max(e, min(nxt, s + 1.5))
        text = " ".join(w["w"] for w in group).upper().replace("{", "(").replace("}", ")")
        lines.append(f"Dialogue: 0,{_ts(s)},{_ts(e)},Cap,,0,0,0,,{{\\pos({cfg['width']//2},{y})}}{text}")
    out.write_text(header + "\n".join(lines) + "\n", encoding="utf-8")


def _pick_bottom() -> Path:
    d = ROOT / SETTINGS["render"]["bottom_dir"]
    files = [p for p in d.iterdir() if p.suffix.lower() in (".mp4", ".mov", ".mkv", ".webm")] if d.exists() else []
    if not files:
        raise RuntimeError(
            f"No bottom videos in {d}. Add your own footage there, or set render.layout: blur in config/settings.yaml."
        )
    return random.choice(files)


def render_clip(source: Path, start: float, end: float, words: list[dict], creator: Creator, out_dir: Path) -> Path:
    cfg = SETTINGS["render"]
    W, H = cfg["width"], cfg["height"]
    dur = end - start
    source, out_dir = Path(source).resolve(), Path(out_dir).resolve()  # ffmpeg runs with cwd=out_dir
    out_dir.mkdir(parents=True, exist_ok=True)
    out = out_dir / "short.mp4"
    (out_dir / "credit.txt").write_text(f"Clip: {creator.credit}", encoding="utf-8")

    font = _font()
    fontopt = f"fontfile='{font}':" if font else ""
    credit = (f"drawtext={fontopt}textfile=credit.txt:fontsize=42:fontcolor=white:borderw=4:bordercolor=black@0.8:"
              f"x=(w-text_w)/2:y=60")

    inputs = ["-ss", f"{start:.3f}", "-t", f"{dur:.3f}", "-i", str(source)]
    layout = cfg.get("layout", "stack")
    if layout == "facecam" and not creator.facecam:
        print(f"    no facecam position for {creator.id} in creators.yaml; using blur layout")
        layout = "blur"
    if layout == "facecam":
        fc = creator.facecam
        top_h = cfg.get("facecam_height", 840)
        bot_h = H - top_h
        cx = cfg.get("crop_center_x", 0.5)
        graph = (
            f"[0:v]split[a][b];"
            f"[a]crop=iw*{fc['w']}:ih*{fc['h']}:iw*{fc['x']}:ih*{fc['y']},"
            f"scale={W}:{top_h}:force_original_aspect_ratio=increase:flags=lanczos,crop={W}:{top_h},setsar=1[cam];"
            f"[b]scale=-2:{bot_h},crop={W}:{bot_h}:'max(0,min(iw-{W},iw*{cx}-{W}/2))':0,setsar=1[game];"
            f"[cam][game]vstack=inputs=2,fps=30[stk]"
        )
        cap_y = top_h
    elif layout == "stack":
        top_h = cfg["top_height"]
        bot_h = H - top_h
        bottom = _pick_bottom()
        bdur = _duration(bottom)
        boff = random.uniform(0, max(0.0, bdur - dur - 1)) if bdur > dur + 1 else 0
        inputs += ["-stream_loop", "-1", "-ss", f"{boff:.2f}", "-i", str(bottom)]
        cx = cfg.get("crop_center_x", 0.5)
        graph = (
            f"[0:v]scale=-2:{top_h},crop={W}:{top_h}:'max(0,min(iw-{W},iw*{cx}-{W}/2))':0,setsar=1[top];"
            f"[1:v]scale={W}:{bot_h}:force_original_aspect_ratio=increase,crop={W}:{bot_h},setsar=1,fps=30[bot];"
            f"[top]fps=30[topf];[topf][bot]vstack=inputs=2[stk]"
        )
        cap_y = top_h
    else:  # blur
        graph = (
            f"[0:v]split[a][b];"
            f"[a]scale={W}:{H}:force_original_aspect_ratio=increase,crop={W}:{H},boxblur=25:2,eq=brightness=-0.15[bg];"
            f"[b]scale={W}:-2[fg];[bg][fg]overlay=0:(H-h)/2,setsar=1,fps=30[stk]"
        )
        cap_y = int(H * 0.72)

    last = "stk"
    if cfg.get("captions", True) and words:
        write_ass(words, start, out_dir / "subs.ass", cap_y)
        graph += f";[{last}]subtitles=subs.ass[cap]"
        last = "cap"
    graph += f";[{last}]{credit}[v]"

    cmd = [
        render_ffmpeg(), "-v", "error", "-stats", "-y", *inputs,
        "-filter_complex", graph, "-map", "[v]", "-map", "0:a?",
        "-t", f"{dur:.3f}", "-c:v", "libx264", "-preset", "medium", "-crf", str(cfg.get("crf", 20)),
        "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "160k", "-movflags", "+faststart", str(out),
    ]
    subprocess.run(cmd, check=True, cwd=out_dir)
    return out
