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
    # groups of up to `per` words, never spanning a pause (or a jump cut)
    groups: list[list[dict]] = []
    for w in words:
        if groups and len(groups[-1]) < per and w["s"] - groups[-1][-1]["e"] < 0.7:
            groups[-1].append(w)
        else:
            groups.append([w])
    lines = []
    for gi, group in enumerate(groups):
        s = group[0]["s"] - clip_start
        e = group[-1]["e"] - clip_start
        nxt = groups[gi + 1][0]["s"] - clip_start if gi + 1 < len(groups) else e + 0.4
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


def _wrap(text: str, width: int = 20) -> str:
    lines, cur = [], ""
    for w in text.split():
        if cur and len(cur) + 1 + len(w) > width:
            lines.append(cur)
            cur = w
        else:
            cur = f"{cur} {w}".strip()
    return "\n".join(lines + [cur])


def plan_segments(start: float, end: float, cuts: list | None) -> list[tuple[float, float]]:
    """Segments to keep (absolute stream seconds), clamped to [start, end]; whole range if no cuts."""
    segs = sorted((max(start, float(a)), min(end, float(b))) for a, b in (cuts or []))
    segs = [(a, b) for a, b in segs if b - a >= 0.5]
    return segs or [(start, end)]


def remap_words(words: list[dict], segs: list[tuple[float, float]]) -> list[dict]:
    """Shift word times onto the edited timeline (0 = first frame of the short); drop words that were cut."""
    out, offset = [], 0.0
    for a, b in segs:
        for w in words:
            if a <= w["s"] < b:
                out.append({**w, "s": w["s"] - a + offset, "e": min(w["e"], b) - a + offset})
        offset += b - a
    return out


def render_clip(source: Path, start: float, end: float, words: list[dict], creator: Creator, out_dir: Path,
                cuts: list | None = None, hook: str | None = None, music: Path | None = None) -> Path:
    cfg = SETTINGS["render"]
    W, H = cfg["width"], cfg["height"]
    segs = plan_segments(start, end, cuts)
    start, end = segs[0][0], segs[-1][1]
    dur = sum(b - a for a, b in segs)
    words = remap_words(words, segs)
    source, out_dir = Path(source).resolve(), Path(out_dir).resolve()  # ffmpeg runs with cwd=out_dir
    out_dir.mkdir(parents=True, exist_ok=True)
    out = out_dir / "short.mp4"
    (out_dir / "credit.txt").write_text(f"Clip: {creator.credit}", encoding="utf-8")

    # jump cuts: keep only the chosen segments, then everything below works on [src]/[aud]
    n = len(segs)
    pre = f"[0:v]split={n}" + "".join(f"[sv{i}]" for i in range(n)) + f";[0:a]asplit={n}" + "".join(f"[sa{i}]" for i in range(n)) + ";"
    for i, (a, b) in enumerate(segs):
        pre += (f"[sv{i}]trim=start={a - start:.3f}:end={b - start:.3f},setpts=PTS-STARTPTS[v{i}];"
                f"[sa{i}]atrim=start={a - start:.3f}:end={b - start:.3f},asetpts=PTS-STARTPTS[a{i}];")
    pre += "".join(f"[v{i}][a{i}]" for i in range(n)) + f"concat=n={n}:v=1:a=1[src][aud0];"
    fmt = "aformat=sample_fmts=fltp:sample_rates=48000:channel_layouts=stereo"
    if music:
        pre += f"[aud0]loudnorm=I=-14:TP=-1.5:LRA=11,aresample=48000,{fmt},asplit=2[voice][sc];"
    else:
        pre += f"[aud0]loudnorm=I=-14:TP=-1.5:LRA=11,aresample=48000,{fmt}[aud];"

    font = _font()
    fontopt = f"fontfile='{font}':" if font else ""
    credit = (f"drawtext={fontopt}textfile=credit.txt:fontsize=42:fontcolor=white:borderw=4:bordercolor=black@0.8:"
              f"x=(w-text_w)/2:y=60")

    inputs = ["-ss", f"{start:.3f}", "-t", f"{end - start:.3f}", "-i", str(source)]  # full span; cuts trim it
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
            f"[src]split[a][b];"
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
            f"[src]scale=-2:{top_h},crop={W}:{top_h}:'max(0,min(iw-{W},iw*{cx}-{W}/2))':0,setsar=1[top];"
            f"[1:v]scale={W}:{bot_h}:force_original_aspect_ratio=increase,crop={W}:{bot_h},setsar=1,fps=30[bot];"
            f"[top]fps=30[topf];[topf][bot]vstack=inputs=2[stk]"
        )
        cap_y = top_h
    else:  # blur
        graph = (
            f"[src]split[a][b];"
            f"[a]scale={W}:{H}:force_original_aspect_ratio=increase,crop={W}:{H},boxblur=25:2,eq=brightness=-0.15[bg];"
            f"[b]scale={W}:-2[fg];[bg][fg]overlay=0:(H-h)/2,setsar=1,fps=30[stk]"
        )
        cap_y = int(H * 0.72)

    last = "stk"
    if cfg.get("captions", True) and words:
        write_ass(words, 0.0, out_dir / "subs.ass", cap_y)
        graph += f";[{last}]subtitles=subs.ass[cap]"
        last = "cap"
    if hook:
        (out_dir / "hook.txt").write_text(_wrap(hook.upper()), encoding="utf-8")
        hook_y = cap_y + 170 if layout != "blur" else int(H * 0.16)
        graph += (f";[{last}]drawtext={fontopt}textfile=hook.txt:fontsize=64:fontcolor=black:line_spacing=10:"
                  f"box=1:boxcolor=white:boxborderw=22:x=(w-text_w)/2:y={hook_y}:enable='lt(t,3.5)'[hk]")
        last = "hk"
    if music:  # quiet background track that ducks further whenever the streamer talks
        mi = inputs.count("-i")
        inputs += ["-stream_loop", "-1", "-i", str(Path(music).resolve())]
        lufs = cfg.get("music_lufs", -22)
        pre += (f"[{mi}:a]loudnorm=I={lufs}:TP=-8:LRA=11,aresample=48000,{fmt},atrim=0:{dur:.3f},afade=t=in:d=0.6,"
                f"afade=t=out:st={max(0.0, dur - 1.5):.3f}:d=1.5[m0];"
                f"[m0][sc]sidechaincompress=threshold=0.05:ratio=2.5:attack=30:release=500[mduck];"
                f"[voice][mduck]amix=inputs=2:duration=first:normalize=0,alimiter=limit=0.89[aud];")
    graph = pre + graph + f";[{last}]{credit}[v]"

    cmd = [
        render_ffmpeg(), "-v", "error", "-stats", "-y", *inputs,
        "-filter_complex", graph, "-map", "[v]", "-map", "[aud]",
        "-t", f"{dur:.3f}", "-c:v", "libx264", "-preset", "medium", "-crf", str(cfg.get("crf", 20)),
        "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "160k", "-movflags", "+faststart", str(out),
    ]
    subprocess.run(cmd, check=True, cwd=out_dir)
    return out
