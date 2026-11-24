"""Never let a key or token leave the process in plain text.

Errors from HTTP libraries can contain the full request URL, and Instagram's Graph API takes the access token as a
URL parameter, so a network error once wrote the token into publish.log (found 24 Nov). Everything we write out —
logs (stdout/stderr), the database, macOS notifications and prompts sent to Claude — goes through `redact()`.
"""
from __future__ import annotations

import io
import json
import os
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
MASK = "<redacted>"

# Shapes of the tokens we use, so a token we have never seen (e.g. a freshly refreshed one) is still caught.
PATTERNS = [
    re.compile(r"((?:access_token|refresh_token|client_secret|api_key|key|code|fb_exchange_token)=)[^&\s'\"]{8,}", re.I),
    re.compile(r"(\"(?:access_token|refresh_token|client_secret|token|api_key)\"\s*:\s*\")[^\"]{8,}", re.I),
    re.compile(r"(Bearer\s+)[A-Za-z0-9._~+/=-]{12,}"),
    re.compile(r"()sk-ant-[A-Za-z0-9_-]{16,}"),            # Anthropic
    re.compile(r"()ya29\.[A-Za-z0-9._-]{16,}"),            # Google access token
    re.compile(r"()1//0[A-Za-z0-9_-]{16,}"),               # Google refresh token
    re.compile(r"()GOCSPX-[A-Za-z0-9_-]{8,}"),             # Google client secret
    re.compile(r"()\b(?:EAA|IGAA|IGQV)[A-Za-z0-9_-]{24,}"),  # Meta / Instagram tokens
    re.compile(r"()\b(?:act|rft)\.[A-Za-z0-9!*_.-]{16,}"),   # TikTok access / refresh tokens
]
_SECRET_NAME = re.compile(r"KEY|SECRET|TOKEN|PASSWORD", re.I)
_cache: tuple[float, list[str]] | None = None


def _known_secrets() -> list[str]:
    """Exact secret values from the environment, .env and secrets/*.json (re-read when those files change)."""
    global _cache
    files = [ROOT / ".env", *sorted((ROOT / "secrets").glob("*.json"))]
    stamp = sum(f.stat().st_mtime for f in files if f.exists())
    if _cache and _cache[0] == stamp:
        return _cache[1]
    vals = {v for k, v in os.environ.items() if _SECRET_NAME.search(k)}
    try:
        for line in (ROOT / ".env").read_text().splitlines():
            if "=" in line and not line.lstrip().startswith("#"):
                k, v = line.split("=", 1)
                if _SECRET_NAME.search(k):
                    vals.add(v.strip().strip("'\""))
    except OSError:
        pass

    def walk(o, key=""):
        if isinstance(o, dict):
            for k, v in o.items():
                walk(v, k)
        elif isinstance(o, list):
            for v in o:
                walk(v, key)
        elif isinstance(o, str) and re.search(r"token|secret|key|password", key, re.I) and "scope" not in key:
            vals.add(o)
    for f in files[1:]:
        try:
            walk(json.loads(f.read_text()))
        except (OSError, ValueError):
            pass
    out = sorted((v for v in vals if len(v) >= 12), key=len, reverse=True)
    _cache = (stamp, out)
    return out


def redact(text):
    if not isinstance(text, str) or len(text) < 8:
        return text
    for s in _known_secrets():
        if s in text:
            text = text.replace(s, MASK)
    for p in PATTERNS:
        text = p.sub(lambda m: m.group(1) + MASK, text)
    return text


class RedactingStream(io.TextIOBase):
    """stdout/stderr wrapper: everything printed (incl. tracebacks) is redacted before it reaches a log file."""

    def __init__(self, inner):
        self._inner = inner

    def write(self, s):
        return self._inner.write(redact(s))

    def flush(self):
        self._inner.flush()

    def isatty(self):
        return self._inner.isatty()

    def fileno(self):
        return self._inner.fileno()

    @property
    def encoding(self):
        return getattr(self._inner, "encoding", "utf-8")


def install() -> None:
    import sys
    if not isinstance(sys.stdout, RedactingStream):
        sys.stdout = RedactingStream(sys.stdout)
    if not isinstance(sys.stderr, RedactingStream):
        sys.stderr = RedactingStream(sys.stderr)
