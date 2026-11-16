"""Standing instructions + feedback for each agent, written by the manager (agent 7).

Every agent appends `brief(<agent>)` to its system prompt, so the manager can coach and upgrade the team over time
by editing config/agents/<agent>.md, without touching code. The "Owner rules" section of those files is never
changed by the manager.
"""
from __future__ import annotations

from .. import db
from ..config import ROOT

DIR = ROOT / "config" / "agents"
AGENTS = {
    "scout": "finds new GTA streams of the permitted creators and decides which recordings to process",
    "transcriber": "transcribes the stream and measures loudness/chat spikes (no LLM; reviewed through its output)",
    "selector": "reads the whole stream, shortlists moments and plans each short (cuts, hook, title, viral chance)",
    "producer": "renders the shorts (camera/avatar detection, layout, captions, music) and runs the QA check",
    "publisher": "posts approved shorts on YouTube, TikTok and Instagram on schedule (no LLM)",
    "analyst": "measures views/likes/comments and rewrites the content playbook",
    "growth": "reviews the three profiles, researches trends and writes each clip's title/caption/hashtags",
    "monetization": "tracks monetization programs and eligibility, flags policy risks, tells the owner when to apply",
}


def path(agent: str):
    return DIR / f"{agent}.md"


def instructions(agent: str) -> str:
    p = path(agent)
    return p.read_text() if p.exists() else ""


def feedback(agent: str, limit: int = 15) -> str:
    with db.connect() as conn:
        rows = conn.execute("SELECT from_agent, message, created_at FROM agent_feedback WHERE to_agent=? "
                            "ORDER BY id DESC LIMIT ?", (agent, limit)).fetchall()
    return "\n".join(f"- [{r['created_at'][:10]} from {r['from_agent']}] {r['message']}" for r in rows)


def brief(agent: str, with_feedback: bool = True) -> str:
    """Text appended to an agent's system prompt."""
    parts = []
    if ins := instructions(agent).strip():
        parts.append(f"# Standing instructions from your manager\n{ins}")
    if with_feedback and (fb := feedback(agent)):
        parts.append(f"# Recent feedback for you\n{fb}")
    return ("\n\n" + "\n\n".join(parts)) if parts else ""
