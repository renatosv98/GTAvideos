"""Agent 4 - Publisher.

Uploads approved clips to the enabled platforms (YouTube now; TikTok and Instagram when enabled), most viral first,
one per publishing slot. Runs every 15 min via launchd (`clipper publish-due`).
The slot logic lives in clipper/scheduler.py.
"""
from __future__ import annotations

from ..scheduler import publish_due, publish_now, reprioritize

__all__ = ["publish_due", "publish_now", "reprioritize", "run"]


def run(dry_run: bool = False) -> None:
    publish_due(dry_run)
