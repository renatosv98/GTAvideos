"""Agent 7 - Manager (the boss of the team).

Daily: reviews what every other agent produced (scout decisions, selector picks vs QA vs real views, producer/QA
results, publisher failures, analyst playbook, growth report), grades each agent, sends each one concrete feedback,
and upgrades the team by rewriting each agent's standing instructions in config/agents/<agent>.md.
It never executes the work itself (no clips, no posts) and never touches the "Owner rules" section, code,
permissions, creators or posting limits. Things only the owner can do are escalated in the report.
History of every instruction change: data/manager_history/.
"""
from __future__ import annotations

import json
import subprocess
from datetime import datetime, timezone

from .. import db, health, llm
from ..config import DATA, ROOT
from . import guidance

REPORT = DATA / "manager_report.md"
HISTORY = DATA / "manager_history"
LAST = DATA / "manager_last.txt"
MAX_INSTRUCTIONS = 1800  # characters of manager instructions per agent: keep prompts focused

SYSTEM = """You are the manager of an AI team that runs GTAuploads, a Spanish GTA 6 shorts channel on YouTube Shorts,
TikTok and Instagram Reels. You are an expert in short-form video editing, virality, audience growth, platform
algorithms, copyright/permission etiquette, and in prompting and coaching AI agents.
You do NOT do the work. You review each agent's recent output against the goal (more views and followers, zero
quality incidents), grade it, give it precise feedback, and upgrade it by rewriting its standing instructions.
Rules for you:
- Evidence first: cite clip numbers, counts, quotes. No generic advice ("be more engaging").
- Instructions you write are read by that agent before every task: imperative, specific, testable, short.
  Keep what works, remove what's obsolete or contradicted by data, fix contradictions between agents.
- Never contradict the Owner rules; if data suggests an owner rule hurts results, escalate it to the owner instead.
- Don't change an agent's instructions without a reason; "no change" is a valid outcome.
- Spot cross-agent problems (e.g. one agent working from stale information another agent has) and fix them.
- You own the team's success: every approved clip must be live on EVERY enabled platform with good packaging.
  Read the publication table; for each gap decide: in progress (leave it), retry it (retry_publish), stop it
  (hold, only if not live anywhere) or improve its text first (repackage). Explain each action.
- Think beyond the current setup: what is missing, what would grow the channel faster (improvements).
- Read the "System state" section before judging: things in progress are not failures."""

SCHEMA = {
    "type": "object",
    "properties": {
        "summary": {"type": "string"},
        "reviews": {"type": "array", "items": {
            "type": "object",
            "properties": {
                "agent": {"type": "string", "enum": list(guidance.AGENTS)},
                "grade": {"type": "string", "enum": ["A", "B", "C", "D", "F", "n/a"]},
                "working": {"type": "string"},
                "problems": {"type": "string"},
                "feedback": {"type": "array", "items": {"type": "string"}},
                "change_instructions": {"type": "boolean"},
                "manager_instructions": {"type": "string"},
                "change_reason": {"type": "string"},
            },
            "required": ["agent", "grade", "working", "problems", "feedback", "change_instructions",
                         "manager_instructions", "change_reason"],
            "additionalProperties": False}},
        "owner_actions": {"type": "array", "items": {"type": "string"}},
        "actions": {"type": "array", "items": {
            "type": "object",
            "properties": {
                "type": {"type": "string", "enum": ["retry_publish", "hold", "repackage", "run_analyst",
                                                    "run_growth_review", "run_market_research"]},
                "clip_id": {"type": "integer"}, "platform": {"type": "string"}, "reason": {"type": "string"},
            },
            "required": ["type", "clip_id", "platform", "reason"], "additionalProperties": False}},
        "improvements": {"type": "array", "items": {
            "type": "object",
            "properties": {"idea": {"type": "string"}, "why": {"type": "string"},
                           "who": {"type": "string", "enum": ["developer", "owner", "agents"]},
                           "impact": {"type": "string", "enum": ["high", "medium", "low"]}},
            "required": ["idea", "why", "who", "impact"], "additionalProperties": False}},
    },
    "required": ["summary", "reviews", "owner_actions", "actions", "improvements"], "additionalProperties": False,
}


def _q(sql: str, *args) -> list[dict]:
    with db.connect() as conn:
        return [dict(r) for r in conn.execute(sql, args)]


def _evidence() -> str:
    """What each agent did recently, as compact text."""
    parts = []
    streams = _q("SELECT id, creator_id, title, duration, status, error, created_at FROM streams ORDER BY id DESC LIMIT 8")
    seen = json.loads((DATA / "seen_vods.json").read_text()) if (DATA / "seen_vods.json").exists() else []
    parts.append("## scout\nStreams processed (newest first):\n" + "\n".join(
        f"- #{s['id']} {s['creator_id']} {s['status']} {(s['title'] or '')[:70]} {('ERROR ' + s['error'][:120]) if s['error'] else ''}"
        for s in streams) + f"\nRecordings skipped as not-GTA so far: {len(seen)}")

    clips = _q("SELECT c.id, s.creator_id, c.title, c.score, c.status, c.review_notes, c.reason, c.edit_json "
               "FROM clips c JOIN streams s ON s.id=c.stream_id ORDER BY c.id DESC LIMIT 30")
    stats = {r["clip_id"]: r for r in _q(
        "SELECT clip_id, MAX(views) AS views, MAX(likes) AS likes, MAX(comments) AS comments, MAX(age_hours) AS age "
        "FROM stats GROUP BY clip_id")} if _q("SELECT name FROM sqlite_master WHERE name='stats'") else {}
    lines = []
    for c in clips:
        e = json.loads(c["edit_json"] or "{}")
        st = stats.get(c["id"])
        perf = f" | {st['views']} views {st['likes']} likes at {st['age']:.0f}h" if st else ""
        lines.append(f"- #{c['id']} {c['creator_id']} [{c['score']}%] {c['status']}{perf} | {c['title']} | "
                     f"hook: {e.get('hook')} | music: {e.get('music_mood')} | camera: "
                     f"{'none' if e.get('facecam') == {} else 'found' if e.get('facecam') else 'default'} | "
                     f"notes: {(c['review_notes'] or '')[:150]} | why picked: {(c['reason'] or '')[:150]}")
    parts.append("## selector + producer (last 30 clips, newest first)\n" + "\n".join(lines))

    ups = _q("SELECT clip_id, platform, status, error, created_at FROM uploads ORDER BY id DESC LIMIT 40")
    fails = [u for u in ups if u["status"] == "failed"]
    by = {}
    for u in ups:
        by.setdefault(u["platform"], {}).setdefault(u["status"], 0)
        by[u["platform"]][u["status"]] += 1
    parts.append("## publisher\nRecent uploads by platform/status: " + json.dumps(by) + "\nFailures:\n" + (
        "\n".join(f"- #{u['clip_id']} {u['platform']}: {(u['error'] or '')[:150]}" for u in fails[:10]) or "(none)")
        + "\nHealth check now:\n" + ("\n".join(f"- {m}" for _, m in health.check()) or "- all good"))

    for name, f in (("analyst", DATA / "analyst_report.md"), ("growth", DATA / "growth_report.md")):
        parts.append(f"## {name} (latest report)\n" + (f.read_text()[:3500] if f.exists() else "(no report yet)"))
    pb = ROOT / "config" / "playbook.md"
    parts.append("## analyst's playbook (what the selector follows)\n" + (pb.read_text()[:3000] if pb.exists() else ""))

    human = _q("SELECT clip_id, message FROM agent_feedback WHERE from_agent IN ('reviewer','user') "
               "ORDER BY id DESC LIMIT 15")
    parts.append("## owner's own feedback\n" + ("\n".join(f"- #{h['clip_id']}: {h['message']}" for h in human)
                                                 or "(none)"))
    return "\n\n".join(parts)


def _split(text: str) -> tuple[str, str]:
    marker = "## Manager instructions"
    if marker in text:
        owner, mgr = text.split(marker, 1)
        return owner.rstrip() + "\n\n", mgr.strip()
    return text.rstrip() + "\n\n", ""


def _ok_pairs() -> set[tuple[int, str]]:
    return {(r["clip_id"], r["platform"]) for r in _q(
        "SELECT clip_id, platform FROM uploads WHERE status IN ('scheduled','published')")}


def reconcile() -> list[str]:
    """Bookkeeping the manager relies on (no LLM, runs every autopilot cycle):
    YouTube 'scheduled' -> 'published' once live; packaging for queued clips that have none."""
    from ..config import SETTINGS
    notes = []
    sched = _q("SELECT u.clip_id, u.remote_id FROM uploads u JOIN clips c ON c.id=u.clip_id WHERE u.platform='youtube' "
               "AND u.status='scheduled' AND c.slot_at < ?", datetime.now(timezone.utc).isoformat())
    if sched:
        try:
            states = health._youtube_states([u["remote_id"] for u in sched])
            flipped = 0
            with db.connect() as conn:
                for u in sched:
                    if states.get(u["remote_id"], {}).get("privacyStatus") == "public":
                        conn.execute("UPDATE uploads SET status='published' WHERE clip_id=? AND platform='youtube'",
                                     (u["clip_id"],))
                        flipped += 1
            if flipped:
                notes.append(f"marked {flipped} YouTube posts as published (they went live on schedule)")
        except Exception as e:
            notes.append(f"could not refresh YouTube status: {e}")
    platforms = [p for p, c in SETTINGS["platforms"].items() if c.get("enabled")]
    ok = _ok_pairs()
    from .growth import stale_text

    def needs(c) -> bool:  # no packaging yet, or packaging/title with dated wording ('quedan 7 días', 'hoy'...)
        pk = json.loads(c["edit_json"] or "{}").get("packaging")
        return not pk or bool(stale_text({k: v for k, v in pk.items() if k != "dated_wording"})) \
            or bool(stale_text({"title": c["title"]})) and not pk

    todo = [c for c in _q("SELECT id, title, edit_json FROM clips WHERE status IN ('approved','published') "
                          "ORDER BY slot_at") if any((c["id"], p) not in ok for p in platforms) and needs(c)]
    from . import growth
    for c in todo[:4]:
        try:
            growth.package(c["id"])
            notes.append(f"packaged #{c['id']} (title/caption/hashtags) before its remaining posts"
                         + push_youtube_text(c["id"]))
        except Exception as e:
            notes.append(f"packaging #{c['id']} failed: {e}")
    return notes


def publication_table() -> str:
    """Every approved/published clip x every enabled platform, with why something isn't live yet."""
    from ..config import SETTINGS
    from ..scheduler import MAX_ATTEMPTS
    platforms = [p for p, c in SETTINGS["platforms"].items() if c.get("enabled")]
    ups = {(u["clip_id"], u["platform"]): u for u in _q("SELECT * FROM uploads")}
    now = datetime.now(timezone.utc)
    rows = ["| clip | slot (UTC) | " + " | ".join(platforms) + " | packaged |", "|---" * (len(platforms) + 3) + "|"]
    for c in _q("SELECT id, slot_at, edit_json FROM clips WHERE status IN ('approved','published') ORDER BY slot_at"):
        slot = datetime.fromisoformat(c["slot_at"]) if c["slot_at"] else None
        cells = []
        for p in platforms:
            u = ups.get((c["id"], p))
            if u and u["status"] in ("scheduled", "published"):
                cells.append(u["status"])
            elif u and u["status"] == "failed":
                gave_up = " (gave up)" if (u["attempts"] or 0) >= MAX_ATTEMPTS else ""
                cells.append(f"FAILED x{u['attempts']}{gave_up}: {(u['error'] or '')[:60]}")
            elif slot and slot > now:
                cells.append("waiting for slot")
            else:
                cells.append("MISSING (queued: catch-up 1 per 3 h / TikTok inbox full / Mac asleep)")
        packed = "yes" if json.loads(c["edit_json"] or "{}").get("packaging") else "no"
        rows.append(f"| #{c['id']} | {(c['slot_at'] or '')[:16]} | " + " | ".join(cells) + f" | {packed} |")
    return "\n".join(rows)


def _state() -> str:
    from ..config import SETTINGS
    from .producer import auto_approve_active
    pl = SETTINGS["platforms"]
    busy = _q("SELECT id, creator_id, status FROM streams WHERE status IN ('new','downloaded','analyzed')")
    q = _q("SELECT MAX(slot_at) AS m FROM clips WHERE status='approved'")[0]["m"]
    busy_txt = ", ".join(f"#{b['id']} {b['creator_id']} ({b['status']})" for b in busy) or "none"
    until = SETTINGS.get("autopilot", {}).get("auto_approve_until")
    return "\n".join([
        f"- Platforms: YouTube {'on' if pl['youtube']['enabled'] else 'off'} (automatic). TikTok "
        f"{'on' if pl['tiktok']['enabled'] else 'off'} in '{pl['tiktok'].get('mode')}' mode: drafts the owner must "
        "tap Post on; TikTok holds max ~5 unposted drafts; app review pending. Instagram "
        f"{'on' if pl.get('instagram', {}).get('enabled') else 'off'}: switched on 16 Nov; older clips catch up at "
        "1 post per 3 h per platform, so gaps on older clips are expected for ~30 h.",
        f"- Streams being processed right now (not stuck): {busy_txt}",
        f"- Auto-approve: {'ON until ' + str(until) if auto_approve_active() else 'OFF (owner reviews)'}",
        f"- Posting queue ends at: {q or 'nothing queued'} (UTC). Cadence: 1 clip every 4 h.",
        "- Everything runs on the owner's Mac; while it sleeps nothing uploads except YouTube pre-scheduled posts.",
        ("- YouTube edit permission: YES (repackage updates YouTube too; hold unschedules it)." if __import__(
            "clipper.uploaders.youtube", fromlist=["can_edit"]).can_edit() else
         "- YouTube edit permission: NOT YET (owner must run `clipper auth youtube`); repackaging only affects "
         "platforms not posted yet."),
    ])


def push_youtube_text(cid: int) -> str:
    """Copy the clip's packaging (title/description/tags) to its YouTube video, if we're allowed to edit."""
    from ..config import load_creators
    from ..scheduler import _description, _yt_tags, _yt_title
    from ..uploaders import youtube
    yt = _q("SELECT remote_id FROM uploads WHERE clip_id=? AND platform='youtube' AND status IN ('scheduled','published')",
            cid)
    if not yt or not youtube.can_edit():
        return ""
    with db.connect() as conn:
        clip = conn.execute("SELECT clips.*, streams.creator_id FROM clips JOIN streams ON streams.id=clips.stream_id "
                            "WHERE clips.id=?", (cid,)).fetchone()
    creator = next(c for c in load_creators() if c.id == clip["creator_id"])
    youtube.update(yt[0]["remote_id"], title=_yt_title(clip), description=_description(clip, creator),
                   tags=_yt_tags(clip))
    return " (YouTube title/description updated too)"


def _act(actions: list[dict]) -> list[str]:
    """Carry out the manager's orders through the other agents, with guard rails."""
    done = []
    for a in actions:
        t, cid, plat = a["type"], a["clip_id"], a["platform"]
        try:
            if t == "retry_publish":
                with db.connect() as conn:
                    n = conn.execute("UPDATE uploads SET attempts=0 WHERE clip_id=? AND platform=? AND status='failed'",
                                     (cid, plat)).rowcount
                done.append(f"retry #{cid} on {plat} ({'failure counter reset' if n else 'queued'}): {a['reason']}")
            elif t == "hold":
                live = _q("SELECT platform FROM uploads WHERE clip_id=? AND status='published'", cid)
                if live:
                    done.append(f"NOT held #{cid}: already live on {', '.join(r['platform'] for r in live)} "
                                f"({a['reason']})")
                    continue
                yt = _q("SELECT remote_id FROM uploads WHERE clip_id=? AND platform='youtube' AND status='scheduled'", cid)
                if yt:
                    from ..uploaders import youtube
                    if youtube.can_edit():
                        youtube.update(yt[0]["remote_id"], private=True)
                        with db.connect() as conn:
                            conn.execute("UPDATE uploads SET status='deleted' WHERE clip_id=? AND platform='youtube'",
                                         (cid,))
                        done.append(f"#{cid} taken off the YouTube schedule (now private)")
                    else:
                        done.append(f"#{cid} is still scheduled on YouTube: owner must delete it in Studio")
                with db.connect() as conn:
                    conn.execute("UPDATE clips SET status='rendered', review_notes=? WHERE id=? AND status='approved'",
                                 (f"held by manager: {a['reason']}", cid))
                done.append(f"held #{cid} for owner review: {a['reason']}")
            elif t == "repackage":
                from . import growth
                growth.package(cid)
                done.append(f"repackaged #{cid}: {a['reason']}" + push_youtube_text(cid))
            elif t == "run_analyst":
                from . import analyst
                analyst.run()
                done.append(f"ran the analyst: {a['reason']}")
            elif t == "run_growth_review":
                from . import growth
                growth.run()
                done.append(f"ran the growth review: {a['reason']}")
            elif t == "run_market_research":
                from . import growth
                growth.research()
                done.append(f"ran market research: {a['reason']}")
        except Exception as e:
            done.append(f"FAILED {t} #{cid}: {e}")
    return done


def run() -> int:
    print("agent 7 · manager: reviewing the team ...")
    housekeeping = reconcile()
    table = publication_table()
    team = "\n\n".join(f"### {a}: {desc}\nCurrent instructions file:\n{guidance.instructions(a) or '(none)'}"
                       for a, desc in guidance.AGENTS.items())
    previous = REPORT.read_text()[:2500] if REPORT.exists() else "(first review)"
    r = llm.ask_json(SYSTEM, (
        f"Today: {datetime.now():%Y-%m-%d %H:%M}.\n\n# The team and their current instructions\n{team}\n\n"
        f"# System state (read first)\n{_state()}\n\n"
        f"# Publication table: every clip x every enabled platform\n{table}\n\n"
        "# Housekeeping just done\n" + ("\n".join(f"- {h}" for h in housekeeping) or "- nothing") + "\n\n"
        f"# Evidence: what the team produced recently\n{_evidence()}\n\n"
        f"# Your previous review (check whether your last changes helped)\n{previous}\n\n"
        "For EVERY agent: grade (n/a if there's nothing to judge yet), what's working, problems, 0-4 feedback "
        "messages addressed to that agent, and whether to change its manager instructions. If yes, return the "
        f"COMPLETE new 'Manager instructions' text (max {MAX_INSTRUCTIONS} characters; bullet points; do not "
        "repeat the owner rules) and the reason. Then owner_actions: things only the owner can do (accounts, "
        "approvals, decisions, conflicts with owner rules). actions: orders to fix publication gaps or quality "
        "problems now (only when needed). improvements: what is missing or could be done better beyond the current "
        "agents (who: developer = code change, owner, agents). Finally a 4-6 sentence summary for the owner."),
        SCHEMA)

    HISTORY.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y-%m-%d_%H%M")
    changed = []
    with db.connect() as conn:
        for rv in r["reviews"]:
            a = rv["agent"]
            for m in rv["feedback"]:
                conn.execute("INSERT INTO agent_feedback (from_agent, to_agent, kind, message) "
                             "VALUES ('manager', ?, 'coaching', ?)", (a, m))
            if rv["change_instructions"] and rv["manager_instructions"].strip():
                p = guidance.path(a)
                old = p.read_text() if p.exists() else ""
                owner, _ = _split(old)
                new = owner + "## Manager instructions\n" + rv["manager_instructions"].strip()[:MAX_INSTRUCTIONS] + "\n"
                if new != old:
                    (HISTORY / f"{stamp}_{a}.md").write_text(old)
                    p.write_text(new)
                    changed.append(a)

    acted = _act(r["actions"])
    lines = [f"# Manager review · {stamp.replace('_', ' ')}", "", r["summary"], "",
             "## Actions taken", ""] + ([f"- {x}" for x in housekeeping + acted] or ["- none"]) + [
             "", "## Publication control", "", table, "", "## Grades", "",
             "| Agent | Grade | Working | Problems |", "|---|---|---|---|"]
    lines += [f"| {v['agent']} | {v['grade']} | {v['working'][:160]} | {v['problems'][:200]} |" for v in r["reviews"]]
    lines += ["", "## Feedback sent"]
    for v in r["reviews"]:
        for m in v["feedback"]:
            lines.append(f"- **→ {v['agent']}:** {m}")
    lines += ["", "## Instructions upgraded"]
    lines += [f"- **{v['agent']}:** {v['change_reason']}" for v in r["reviews"] if v["agent"] in changed] or ["- none"]
    lines += ["", "## Needs you (owner)"] + ([f"- {x}" for x in r["owner_actions"]] or ["- nothing"])
    order = {"high": 0, "medium": 1, "low": 2}
    lines += ["", "## What's missing / could be better"] + [
        f"- **[{i['impact'].upper()} · {i['who']}]** {i['idea']} ({i['why']})"
        for i in sorted(r["improvements"], key=lambda i: order[i["impact"]])]
    REPORT.write_text("\n".join(lines) + "\n")
    LAST.write_text(datetime.now(timezone.utc).isoformat())
    print(f"    {len(r['reviews'])} agents reviewed · instructions upgraded: {', '.join(changed) or 'none'} "
          f"· report: {REPORT.relative_to(ROOT)}")
    subprocess.run(["osascript", "-e", f"display notification {json.dumps(r['summary'][:200])} "
                                       f"with title \"GTAuploads manager\""], check=False)
    return 0


def due(hours: float = 12) -> bool:
    try:
        return (datetime.now(timezone.utc) - datetime.fromisoformat(LAST.read_text().strip())
                ).total_seconds() > hours * 3600
    except (OSError, ValueError):
        return True
