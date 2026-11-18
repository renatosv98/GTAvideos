"""Agent 8 - Monetization.

Weekly (and whenever the manager asks): researches the CURRENT monetization programs and rules of YouTube, TikTok and
Instagram on the web, measures where the channel stands against each requirement, flags policy risks for a clips
channel (reused content, originality, creator permission terms, music licences), steers the other agents towards
eligibility, and tells the owner exactly what to do and when (apply, link payout account, tax forms).

It never enters, stores or asks for banking, tax or identity data: sign-ups and payouts are done by the owner in
the platforms' own apps. Report: data/monetization_report.md (+ Mac notification; a loud one when eligible).
"""
from __future__ import annotations

import json
import subprocess
from datetime import datetime, timedelta, timezone

import requests

from .. import db, llm
from ..config import DATA, ROOT, load_creators
from .guidance import brief

REPORT = DATA / "monetization_report.md"
BRIEF = DATA / "monetization_brief.json"
LAST = DATA / "monetization_last.txt"

SYSTEM = """You are the monetization lead of GTAuploads, a Spanish-language GTA 6 shorts channel on YouTube Shorts,
TikTok and Instagram Reels that clips livestreams of IlloJuan, Rubius and Auronplay with their permission (vertical
re-edits: face cam + gameplay, jump cuts, hook text, captions, CC-BY background music). You are an expert in every
platform's monetization programs, eligibility rules, payout setup, and the policies that block clip channels
(reused/unoriginal content, copyright claims, originality requirements, minimum video length, regional availability).
Goal: start earning as early as possible and maximise revenue, without risking the accounts or the creators' trust.
Be concrete: numbers, gaps, dates, exact steps. Use only rules you verified in the research brief; say when unsure."""

BRIEF_SCHEMA = {
    "type": "object",
    "properties": {
        "as_of": {"type": "string"},
        "programs": {"type": "array", "items": {
            "type": "object",
            "properties": {
                "platform": {"type": "string", "enum": ["youtube", "tiktok", "instagram"]},
                "program": {"type": "string"},
                "requirements": {"type": "array", "items": {"type": "string"}},
                "pays_for": {"type": "string"},
                "clip_channel_risks": {"type": "string"},
                "how_to_apply": {"type": "string"},
                "payout_setup": {"type": "string"},
                "available_in": {"type": "string"},
            },
            "required": ["platform", "program", "requirements", "pays_for", "clip_channel_risks", "how_to_apply",
                         "payout_setup", "available_in"], "additionalProperties": False}},
        "other_income": {"type": "array", "items": {"type": "string"}},
        "sources": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["as_of", "programs", "other_income", "sources"], "additionalProperties": False,
}

SCHEMA = {
    "type": "object",
    "properties": {
        "summary": {"type": "string"},
        "eligible_now": {"type": "array", "items": {"type": "string"}},
        "progress": {"type": "array", "items": {
            "type": "object",
            "properties": {
                "platform": {"type": "string"}, "program": {"type": "string"},
                "requirement": {"type": "string"}, "current": {"type": "string"},
                "gap": {"type": "string"}, "eta": {"type": "string"},
            },
            "required": ["platform", "program", "requirement", "current", "gap", "eta"],
            "additionalProperties": False}},
        "risks": {"type": "array", "items": {"type": "string"}},
        "owner_actions": {"type": "array", "items": {"type": "string"}},
        "agent_feedback": {"type": "array", "items": {
            "type": "object",
            "properties": {"to": {"type": "string", "enum": ["selector", "producer", "growth", "publisher",
                                                             "manager"]},
                           "message": {"type": "string"}},
            "required": ["to", "message"], "additionalProperties": False}},
        "revenue_ideas": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["summary", "eligible_now", "progress", "risks", "owner_actions", "agent_feedback",
                 "revenue_ideas"], "additionalProperties": False,
}


def research() -> dict:
    print("agent 8 · monetization: researching current programs (web) ...")
    r = llm.ask_json(SYSTEM + brief("monetization", with_feedback=False), (
        f"Today is {datetime.now():%Y-%m-%d}. Research the web now (official help pages first: YouTube Partner "
        "Program / Shorts revenue sharing, TikTok Creator Rewards / Subscriptions / LIVE gifts / Creator "
        "Marketplace, Instagram bonuses / gifts / subscriptions / Creator Marketplace). For each program: current "
        "eligibility requirements (followers, views and their time window, video length, account age, region), "
        "what it pays for, specific risks for a channel of re-edited stream clips (reused/unoriginal content "
        "policies, how they are judged, what makes clips count as 'transformative'), how to apply, payout setup "
        "(AdSense, tax info), availability in Spain/UK. Also other income for a clips channel (sponsorships, "
        "affiliate, revenue share with the creators). Cite sources."), BRIEF_SCHEMA, web=True, effort="medium")
    BRIEF.write_text(json.dumps(r, ensure_ascii=False, indent=1))
    return r


def _metrics() -> dict:
    out: dict = {}
    try:
        from googleapiclient.discovery import build

        from ..uploaders import youtube
        yt = build("youtube", "v3", credentials=youtube._creds(), cache_discovery=False)
        ch = yt.channels().list(part="statistics,snippet,status", mine=True).execute()["items"][0]
        with db.connect() as conn:
            ids = [r["remote_id"] for r in conn.execute(
                "SELECT u.remote_id FROM uploads u JOIN clips c ON c.id=u.clip_id WHERE u.platform='youtube' "
                "AND u.status='published' AND c.slot_at > ?", ((datetime.now(timezone.utc) - timedelta(days=90))
                                                             .isoformat(),))]
        v90 = 0
        for i in range(0, len(ids), 50):
            for v in yt.videos().list(part="statistics", id=",".join(ids[i:i + 50])).execute().get("items", []):
                v90 += int(v["statistics"].get("viewCount", 0))
        out["youtube"] = {"subscribers": int(ch["statistics"].get("subscriberCount", 0)),
                          "total_views": int(ch["statistics"].get("viewCount", 0)),
                          "videos": int(ch["statistics"].get("videoCount", 0)),
                          "shorts_views_last_90_days": v90, "channel_created": ch["snippet"].get("publishedAt"),
                          "country": ch["snippet"].get("country"),
                          "note": "watch hours of long videos: 0 (Shorts only)"}
    except Exception as e:
        out["youtube"] = {"error": str(e)[:150]}
    try:
        from ..uploaders import instagram as ig
        tok = ig._token()
        d = ig._check(requests.get(f"{ig._host()}/{ig._v()}/{tok['user_id']}", params={
            "fields": "followers_count,media_count", "access_token": tok["access_token"]}, timeout=30), "profile")
        out["instagram"] = {"followers": d.get("followers_count"), "posts": d.get("media_count")}
    except Exception as e:
        out["instagram"] = {"error": str(e)[:150]}
    with db.connect() as conn:
        tt = conn.execute("SELECT COUNT(*) FROM uploads WHERE platform='tiktok' AND status='published'").fetchone()[0]
        lengths = [r[0] for r in conn.execute("SELECT end - start FROM clips WHERE status='published'")]
    out["tiktok"] = {"posts_sent": tt, "followers": "unknown (needs user.info.stats scope after app review)"}
    out["our_clips"] = {"count": len(lengths), "max_source_span_seconds": round(max(lengths, default=0)),
                        "typical_length_seconds": "22-43 (all under 60 s)"}
    return out


def run() -> int:
    if not BRIEF.exists() or (datetime.now().timestamp() - BRIEF.stat().st_mtime) > 7 * 86400:
        research()
    print("agent 8 · monetization: checking where we stand ...")
    metrics = _metrics()
    creators = [{"creator": c.display_name, "permission": c.permission.get("source"),
                 "terms": c.permission.get("terms") or "(not recorded: monetization allowed? unknown)"}
                for c in load_creators()]
    analyst = (DATA / "analyst_report.md").read_text()[:2000] if (DATA / "analyst_report.md").exists() else ""
    previous = REPORT.read_text()[:2000] if REPORT.exists() else "(first report)"
    r = llm.ask_json(SYSTEM + brief("monetization"), (
        f"Today: {datetime.now():%Y-%m-%d}.\n\n# Verified program rules (research brief)\n{BRIEF.read_text()[:9000]}"
        f"\n\n# Our numbers now\n{json.dumps(metrics, ensure_ascii=False, indent=1)}\n\n"
        f"# Creator permissions on record\n{json.dumps(creators, ensure_ascii=False)}\n\n"
        f"# Performance (analyst)\n{analyst}\n\n# Your previous report\n{previous}\n\n"
        "Tasks: summary (3-5 sentences); eligible_now (programs we can apply to today, else empty); progress "
        "(every relevant requirement: current vs needed, gap, realistic ETA at current growth); risks (what could "
        "block or claw back monetization for this channel, most serious first); owner_actions (exact steps only "
        "the owner can do, in order, incl. confirming with the creators that monetized clips are OK); "
        "agent_feedback (concrete changes to content/format/packaging/cadence that improve eligibility or revenue, "
        "addressed to the right agent); revenue_ideas (beyond platform payouts)."), SCHEMA)

    with db.connect() as conn:
        for f in r["agent_feedback"]:
            conn.execute("INSERT INTO agent_feedback (from_agent, to_agent, kind, message) "
                         "VALUES ('monetization', ?, 'monetization', ?)", (f["to"], f["message"]))
    stamp = datetime.now().strftime("%Y-%m-%d %H:%M")
    lines = [f"# Monetization report · {stamp}", "", r["summary"], "",
             "## Eligible now", ""] + ([f"- ✅ {x}" for x in r["eligible_now"]] or ["- nothing yet"])
    lines += ["", "## Progress to each program", "", "| Platform | Program | Requirement | Now | Gap | ETA |",
              "|---|---|---|---|---|---|"]
    lines += [f"| {p['platform']} | {p['program']} | {p['requirement']} | {p['current']} | {p['gap']} | {p['eta']} |"
              for p in r["progress"]]
    lines += ["", "## Risks", ""] + [f"- ⚠️ {x}" for x in r["risks"]]
    lines += ["", "## What you need to do", ""] + [f"{i}. {x}" for i, x in enumerate(r["owner_actions"], 1)]
    lines += ["", "## Changes sent to the team", ""] + [f"- **→ {f['to']}:** {f['message']}" for f in r["agent_feedback"]]
    lines += ["", "## More revenue ideas", ""] + [f"- {x}" for x in r["revenue_ideas"]]
    lines += ["", "_Sources: see data/monetization_brief.json_", ""]
    REPORT.write_text("\n".join(lines))
    LAST.write_text(datetime.now(timezone.utc).isoformat())
    print(f"    report → {REPORT.relative_to(ROOT)}")
    title = "💰 GTAuploads: you can apply for monetization!" if r["eligible_now"] else "GTAuploads monetization"
    subprocess.run(["osascript", "-e", f"display notification {json.dumps(r['summary'][:200], ensure_ascii=False)} "
                                       f"with title {json.dumps(title, ensure_ascii=False)} sound name \"Glass\""], check=False)
    return 0


def due(days: float = 7) -> bool:
    try:
        return (datetime.now(timezone.utc) - datetime.fromisoformat(LAST.read_text().strip())
                ).total_seconds() > days * 86400
    except (OSError, ValueError):
        return True
