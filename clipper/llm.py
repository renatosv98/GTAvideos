"""One door to Claude for every agent.

backend: claude_code (default) runs the Claude Code CLI headless, logged in with your Claude subscription,
         so it uses your plan's usage limits instead of API credits.
backend: api uses the Anthropic API with ANTHROPIC_API_KEY (pay-as-you-go credits).
"""
from __future__ import annotations

import base64
import glob
import json
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
             model: str | None = None, effort: str | None = None) -> dict[str, Any]:
    """Send a prompt (+ optional images) and get back JSON matching `schema`."""
    cfg = _cfg()
    model = model or cfg.get("model", "claude-opus-5-5")
    effort = effort or cfg.get("effort", "medium")
    backend = cfg.get("backend", "claude_code")
    tries = int(cfg.get("retries", 3))
    for attempt in range(1, tries + 1):
        try:
            if backend == "api":
                return _api(system, prompt, schema, images or [], model, effort)
            return _claude_code(system, prompt, schema, images or [], model, effort)
        except LLMError as e:
            if attempt == tries:
                raise
            wait = int(cfg.get("retry_wait_seconds", 120)) * attempt
            print(f"    Claude call failed ({e}); retrying in {wait}s ({attempt}/{tries - 1})")
            time.sleep(wait)
    raise AssertionError("unreachable")


def _claude_code(system, prompt, schema, images, model, effort) -> dict:
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
               "--max-turns", str(len(names) + 5)]
        cmd += ["--tools", "Read", "--allowedTools", "Read"] if names else ["--tools", ""]
        env = {k: v for k, v in os.environ.items() if k != "ANTHROPIC_API_KEY"}  # force subscription login
        proc = subprocess.run(cmd, input=prompt, capture_output=True, text=True, cwd=tmpd, env=env,
                              timeout=int(_cfg().get("timeout_seconds", 1800)))
    try:
        out = json.loads(proc.stdout)
    except json.JSONDecodeError:
        raise LLMError(f"Claude Code returned no JSON (exit {proc.returncode}): {(proc.stderr or proc.stdout)[-400:]}")
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
