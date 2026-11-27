"""One door to Claude for every agent.

backend: claude_code (default) runs the Claude Code CLI headless, logged in with your Claude subscription,
         so it uses your plan's usage limits instead of API credits.
backend: api uses the Anthropic API with ANTHROPIC_API_KEY (pay-as-you-go credits).
"""
from __future__ import annotations

import base64
import glob
import json
import re
import os
import shutil
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Any

from .config import SETTINGS


class LLMError(RuntimeError):
    pass


def _cfg() -> dict:
    return SETTINGS.get("llm", {})


def claude_binary() -> str:
    path = _cfg().get("claude_path") or shutil.which("claude")
    if path:
        return path
    # Claude Code bundled with the VS Code extension (newest version first)
    found = sorted(glob.glob(os.path.expanduser(
        "~/.vscode/extensions/anthropic.claude-code-*/resources/native-binary/claude")), reverse=True)
    if not found:
        raise LLMError("Claude Code not found. Install it (https://claude.com/claude-code) or set llm.claude_path.")
    return found[0]


def ask_json(system: str, prompt: str, schema: dict, images: list[Path] | None = None,
             model: str | None = None, effort: str | None = None, web: bool = False) -> dict[str, Any]:
    """Send a prompt (+ optional images) and get back JSON matching `schema`.
    web=True lets Claude search and read the web first (market/trend research; Claude Code backend only)."""
    from .redact import redact
    system, prompt = redact(system), redact(prompt)  # agents read logs/errors; never pass a token to Claude
    cfg = _cfg()
    model = model or cfg.get("model", "claude-opus-5-5")
    effort = effort or cfg.get("effort", "medium")
    backend = cfg.get("backend", "claude_code")
    tries = int(cfg.get("retries", 3))
    for attempt in range(1, tries + 1):
        try:
            if backend == "api":
                return _api(system, prompt, schema, images or [], model, effort)
            return _claude_code(system, prompt, schema, images or [], model, effort, web)
        except LLMError as e:
            if attempt == tries:
                raise
            wait = int(cfg.get("retry_wait_seconds", 120)) * attempt
            print(f"    Claude call failed ({e}); retrying in {wait}s ({attempt}/{tries - 1})")
            time.sleep(wait)
    raise AssertionError("unreachable")


_SECRET_ENV = re.compile(r"KEY|SECRET|TOKEN|PASSWORD|INSTAGRAM|TIKTOK", re.I)


def _caller() -> str:
    """Which agent made the call (first frame outside this module), e.g. 'qa.review'."""
    import inspect
    for f in inspect.stack()[2:]:
        mod = f.frame.f_globals.get("__name__", "")
        if mod.startswith("clipper") and mod != __name__:
            return f"{mod.rsplit('.', 1)[-1]}.{f.function}"
    return "?"


def _log_usage(out: dict, model: str, effort: str, images: int) -> None:
    """One line per Claude call in data/llm_usage.jsonl, so the token cost of each agent is visible."""
    try:
        from datetime import datetime, timezone
        from .config import DATA
        u = out.get("usage") or {}
        row = {"at": datetime.now(timezone.utc).isoformat(timespec="seconds"), "agent": _caller(), "model": model,
               "effort": effort, "images": images, "cost_usd": out.get("total_cost_usd"),
               "in": u.get("input_tokens"), "cache_read": u.get("cache_read_input_tokens"),
               "cache_write": u.get("cache_creation_input_tokens"), "out": u.get("output_tokens"),
               "ok": not out.get("is_error")}
        with open(DATA / "llm_usage.jsonl", "a") as f:
            f.write(json.dumps(row) + "\n")
    except Exception:
        pass  # logging must never break an agent


def _claude_code(system, prompt, schema, images, model, effort, web=False) -> dict:
    with tempfile.TemporaryDirectory(prefix="clipper-llm-") as tmp:
        tmpd = Path(tmp)
        names = []
        for i, img in enumerate(images):
            dst = tmpd / f"frame_{i:03d}{Path(img).suffix or '.jpg'}"
            shutil.copy(img, dst)
            names.append(dst.name)
        if names:
            prompt += ("\n\nStill frames are saved in the current directory. Look at each with the Read tool before "
                       "answering: " + ", ".join(names))
        cmd = [claude_binary(), "-p", "--output-format", "json", "--json-schema", json.dumps(schema),
               "--model", model, "--effort", effort, "--system-prompt", system,
               "--no-session-persistence", "--strict-mcp-config", "--setting-sources", "",
               "--max-turns", str(len(names) + (40 if web else 5))]
        tools = (["Read"] if names else []) + (["WebSearch", "WebFetch"] if web else [])
        # Prompts contain text from the internet (chat, titles, transcripts). Even if one tries to steer Claude,
        # it can only read the frames in its own temp folder: never the home folder (.env, secrets/, ~/.ssh).
        cmd += ["--tools", ",".join(tools), "--allowedTools", ",".join(tools),
                "--disallowedTools", "Read(~/**),Read(//Users/**),Read(//etc/**)"] if tools else ["--tools", ""]
        # force subscription login, and pass no keys/tokens at all to the subprocess
        env = {k: v for k, v in os.environ.items() if not _SECRET_ENV.search(k)}
        proc = subprocess.run(cmd, input=prompt, capture_output=True, text=True, cwd=tmpd, env=env,
                              timeout=int(_cfg().get("timeout_seconds", 1800)))
    try:
        out = json.loads(proc.stdout)
    except json.JSONDecodeError:
        raise LLMError(f"Claude Code returned no JSON (exit {proc.returncode}): {(proc.stderr or proc.stdout)[-400:]}")
    _log_usage(out, model, effort, len(names))
    if out.get("is_error") or out.get("structured_output") is None:
        raise LLMError(f"{out.get('subtype')}: {str(out.get('result'))[:400]}")
    return out["structured_output"]


def _api(system, prompt, schema, images, model, effort) -> dict:
    import anthropic

    content: list[dict] = [{"type": "text", "text": prompt}]
    for img in images:
        content.append({"type": "image", "source": {"type": "base64", "media_type": "image/jpeg",
                                                    "data": base64.b64encode(Path(img).read_bytes()).decode()}})
    try:
        with anthropic.Anthropic().beta.messages.stream(
            model=model, max_tokens=32000, system=system,
            messages=[{"role": "user", "content": content}],
            thinking={"type": "adaptive"},
            output_config={"effort": effort, "format": {"type": "json_schema", "schema": schema}},
            betas=["server-side-fallback-2026-07-01"], fallbacks="default",
        ) as stream:
            msg = stream.get_final_message()
    except anthropic.APIStatusError as e:
        if e.status_code in (429, 500, 502, 503, 529):
            raise LLMError(str(e))
        raise
    if msg.stop_reason == "refusal":
        raise RuntimeError("Claude declined this request.")
    return json.loads(next(b.text for b in msg.content if b.type == "text"))
