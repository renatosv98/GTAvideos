"""Agent 6 - Growth (profile awareness).

Weekly: reads the YouTube channel, Instagram and TikTok profiles (name, bio/description, links, profile picture,
banner, numbers, recent posts), looks at the images, compares the three for consistency, and writes concrete,
ready-to-paste recommendations to data/growth_report.md (+ a Mac notification).

The APIs we're allowed to use can't edit profiles, so this agent recommends; you apply.
"""
from __future__ import annotations

import json
import subprocess
import tempfile
from datetime import datetime, timezone
from pathlib import Path

import requests

from .guidance import brief
from .. import db, llm
from ..config import DATA, ROOT, SETTINGS

REPORT = DATA / "growth_report.md"
LAST = DATA / "growth_last.txt"

SYSTEM = """You are the growth strategist of GTAuploads, a Spanish-language GTA 6 shorts channel (YouTube Shorts,
TikTok, Instagram Reels) that clips streams from IlloJuan, Rubius and Auronplay with their permission.
Goal: maximise awareness and follower growth of the three profiles. You know how each platform's profile,
search and recommendation work (YouTube handle/description/keywords/links/banner/playlists, TikTok bio limits
and link rules, Instagram bio/links/highlights/category). Recommendations must be specific, ready to paste,
respect each platform's limits, and never impersonate the creators or Rockstar (no official logos, no
"official" wording). Prioritise by expected impact vs effort."""

SCHEMA = {
    "type": "object",
    "properties": {
        "summary": {"type": "string"},
        "score": {"type": "integer"},
        "recommendations": {"type": "array", "items": {
            "type": "object",
            "properties": {
                "platform": {"type": "string", "enum": ["youtube", "tiktok", "instagram", "all"]},
                "field": {"type": "string"},
                "current": {"type": "string"},
                "paste_this": {"type": "string"},
                "why": {"type": "string"},
                "priority": {"type": "string", "enum": ["high", "medium", "low"]},
                "how": {"type": "string"},
            },
            "required": ["platform", "field", "current", "paste_this", "why", "priority", "how"],
            "additionalProperties": False}},
        "consistency_issues": {"type": "array", "items": {"type": "string"}},
        "image_feedback": {"type": "string"},
    },
    "required": ["summary", "score", "recommendations", "consistency_issues", "image_feedback"],
    "additionalProperties": False,
}


def _youtube() -> dict:
    from googleapiclient.discovery import build

    from ..uploaders import youtube
    yt = build("youtube", "v3", credentials=youtube._creds(), cache_discovery=False)
    ch = yt.channels().list(part="snippet,brandingSettings,statistics,contentDetails", mine=True).execute()["items"][0]
    sn, br = ch["snippet"], ch.get("brandingSettings", {})
    uploads = ch["contentDetails"]["relatedPlaylists"]["uploads"]
    vids = yt.playlistItems().list(part="snippet", playlistId=uploads, maxResults=10).execute().get("items", [])
    pls = yt.playlists().list(part="snippet", mine=True, maxResults=20).execute().get("items", [])
    return {
        "name": sn["title"], "handle": sn.get("customUrl"), "description": sn.get("description", ""),
        "country": sn.get("country"), "keywords": br.get("channel", {}).get("keywords", ""),
        "has_banner": bool(br.get("image", {}).get("bannerExternalUrl")),
        "stats": ch.get("statistics", {}), "playlists": [p["snippet"]["title"] for p in pls],
        "recent_titles": [v["snippet"]["title"] for v in vids],
        "_avatar": sn["thumbnails"].get("high", sn["thumbnails"]["default"])["url"],
        "_banner": br.get("image", {}).get("bannerExternalUrl"),
        "note": "Channel links (header links) are not readable through the API; check them by hand.",
    }


def _instagram() -> dict:
    from ..uploaders import instagram as ig
    tok = ig._token()
    d = ig._check(requests.get(f"{ig._host()}/{ig._v()}/{tok['user_id']}", params={
        "fields": "username,name,biography,website,profile_picture_url,followers_count,follows_count,media_count",
        "access_token": tok["access_token"]}, timeout=30), "profile")
    media = ig._check(requests.get(f"{ig._host()}/{ig._v()}/{tok['user_id']}/media", params={
        "fields": "caption,like_count,comments_count,timestamp", "limit": 10,
        "access_token": tok["access_token"]}, timeout=30), "media").get("data", [])
    return {"username": d.get("username"), "name": d.get("name"), "bio": d.get("biography", ""),
            "website": d.get("website"), "followers": d.get("followers_count"), "posts": d.get("media_count"),
            "recent_captions": [(m.get("caption") or "")[:150] for m in media],
            "_avatar": d.get("profile_picture_url")}


def _tiktok() -> dict:
    from ..uploaders import tiktok
    token = tiktok._token()
    r = requests.get(f"{tiktok.API}/user/info/", headers={"Authorization": f"Bearer {token}"}, params={
        "fields": "open_id,username,display_name,bio_description,avatar_url,profile_deep_link,is_verified"},
        timeout=30).json()
    u = r.get("data", {}).get("user", {})
    return {"username": u.get("username"), "name": u.get("display_name"), "bio": u.get("bio_description", ""),
            "verified": u.get("is_verified"), "_avatar": u.get("avatar_url"),
            "note": "Follower/like counts need the user.info.stats scope (not granted yet). TikTok's API can't "
                    "show whether the YouTube/Instagram buttons are linked; check by hand."}


def _image(url: str | None, out: Path) -> Path | None:
    if not url:
        return None
    try:
        r = requests.get(url, timeout=30)
        r.raise_for_status()
        raw = out.with_suffix(".src")
        raw.write_bytes(r.content)
        subprocess.run(["ffmpeg", "-v", "error", "-y", "-i", str(raw), "-vf", "scale='min(1280,iw)':-2",
                        str(out)], check=True)
        return out
    except Exception:
        return None


def collect() -> dict:
    out = {}
    for name, fn in (("youtube", _youtube), ("instagram", _instagram), ("tiktok", _tiktok)):
        try:
            out[name] = fn()
        except Exception as e:
            out[name] = {"error": str(e)[:200]}
    return out


def run() -> int:
    print("agent 6 · growth: reading the three profiles ...")
    profiles = collect()
    analyst = (DATA / "analyst_report.md").read_text() if (DATA / "analyst_report.md").exists() else "(none yet)"
    playbook = (ROOT / "config" / "playbook.md").read_text() if (ROOT / "config" / "playbook.md").exists() else ""
    with db.connect() as conn:
        published = conn.execute("SELECT COUNT(*) FROM clips WHERE status='published'").fetchone()[0]
    previous = REPORT.read_text()[:3000] if REPORT.exists() else "(first report)"

    with tempfile.TemporaryDirectory() as d:
        images, labels = [], []
        for plat, key in (("youtube", "_avatar"), ("youtube", "_banner"), ("instagram", "_avatar"),
                          ("tiktok", "_avatar")):
            p = _image(profiles.get(plat, {}).get(key), Path(d) / f"{plat}{key}.jpg")
            if p:
                images.append(p)
                labels.append(f"{plat} {'banner' if key == '_banner' else 'profile picture'}")
        clean = {k: {kk: vv for kk, vv in v.items() if not kk.startswith("_")} for k, v in profiles.items()}
        r = llm.ask_json(SYSTEM + brief("growth"), (
            f"Today: {datetime.now():%Y-%m-%d}. Shorts published so far: {published}. "
            f"Posting cadence: one clip every 4 h on all three platforms.\n\n"
            f"# Profiles (from the APIs)\n{json.dumps(clean, ensure_ascii=False, indent=1)}\n\n"
            f"# Images attached, in order: {', '.join(labels) or 'none'}\n\n"
            f"# What's performing (analyst)\n{analyst[:2500]}\n\n# Content playbook\n{playbook[:2000]}\n\n"
            f"# Your previous report (don't repeat what's already applied; follow up on what isn't)\n{previous}\n\n"
            "Tasks: (1) summary for the owner, 3-4 sentences; (2) score 0-100 for how well the profiles are set up "
            "for awareness; (3) recommendations: 6-15 items across bio/description, name/handle, profile picture, "
            "banner, links (cross-links between the three, link-in-bio rules per platform), keywords/SEO, "
            "playlists/highlights/pinned posts, posting/caption habits that drive profile visits. Each with the "
            "exact text to paste (respect character limits: TikTok bio 80, Instagram bio 150, YouTube description "
            "1000) and where to click; (4) consistency issues between the three profiles; (5) feedback on the "
            "images."), SCHEMA, images=images or None)

    stamp = datetime.now().strftime("%Y-%m-%d %H:%M")
    order = {"high": 0, "medium": 1, "low": 2}
    recs = sorted(r["recommendations"], key=lambda x: order[x["priority"]])
    lines = [f"# Growth report · {stamp}", "", f"**Profile score: {r['score']}/100**", "", r["summary"], "",
             "## Recommendations (most impact first)", ""]
    for i, x in enumerate(recs, 1):
        lines += [f"### {i}. [{x['priority'].upper()}] {x['platform'].capitalize()} · {x['field']}",
                  f"- **Now:** {x['current'] or '(empty)'}", f"- **Why:** {x['why']}", f"- **Where:** {x['how']}",
                  "- **Paste this:**", "", "```", x["paste_this"], "```", ""]
    lines += ["## Consistency between profiles", ""] + [f"- {c}" for c in r["consistency_issues"]]
    lines += ["", "## Profile pictures and banner", "", r["image_feedback"], ""]
    REPORT.write_text("\n".join(lines))
    LAST.write_text(datetime.now(timezone.utc).isoformat())
    print(f"    score {r['score']}/100 · {len(recs)} recommendations → {REPORT.relative_to(ROOT)}")
    subprocess.run(["osascript", "-e", f"display notification {json.dumps(r['summary'][:200])} "
                                       f"with title \"GTAuploads growth ({r['score']}/100)\""], check=False)
    return 0


def due(days: float = 7) -> bool:
    try:
        return (datetime.now(timezone.utc) - datetime.fromisoformat(LAST.read_text().strip())
                ).total_seconds() > days * 86400
    except (OSError, ValueError):
        return True


# ---------------------------------------------------------------- market research + per-clip packaging

MARKET = DATA / "market_brief.json"

MARKET_SCHEMA = {
    "type": "object",
    "properties": {
        "as_of": {"type": "string"},
        "trends": {"type": "array", "items": {"type": "string"}},
        "hashtags": {"type": "object", "properties": {
            p: {"type": "array", "items": {"type": "string"}} for p in ("youtube", "tiktok", "instagram")},
            "required": ["youtube", "tiktok", "instagram"], "additionalProperties": False},
        "avoid_hashtags": {"type": "array", "items": {"type": "string"}},
        "title_patterns": {"type": "array", "items": {"type": "string"}},
        "caption_rules": {"type": "object", "properties": {
            p: {"type": "string"} for p in ("youtube", "tiktok", "instagram")},
            "required": ["youtube", "tiktok", "instagram"], "additionalProperties": False},
        "upcoming_moments": {"type": "array", "items": {"type": "string"}},
        "sources": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["as_of", "trends", "hashtags", "avoid_hashtags", "title_patterns", "caption_rules",
                 "upcoming_moments", "sources"], "additionalProperties": False,
}


def research() -> dict:
    """Web research on what's working right now for Spanish GTA 6 short-form content (runs every ~3 days)."""
    print("agent 6 · growth: researching the market (web) ...")
    r = llm.ask_json(SYSTEM + brief("growth", with_feedback=False), (
        f"Today is {datetime.now():%Y-%m-%d}. Research the web NOW (search several times, prefer sources from the "
        "last 2 weeks): what is working for Spanish-language GTA 6 / GTA short videos on YouTube Shorts, TikTok "
        "and Instagram Reels, especially clips of IlloJuan, Rubius and Auronplay. Find: current trends and "
        "talking points (launch, leaks, memes, events), hashtags in use per platform (mix of big + niche, "
        "Spanish + English), hashtags to avoid (banned, spammy, irrelevant), title/hook patterns that get "
        "views, caption rules per platform (length, keyword placement, CTA), upcoming dates worth riding. "
        "Cite your sources."), MARKET_SCHEMA, web=True, effort="medium")
    MARKET.write_text(json.dumps(r, ensure_ascii=False, indent=1))
    print(f"    {len(r['trends'])} trends, hashtags for 3 platforms → {MARKET.relative_to(ROOT)}")
    return r


def research_due(days: float = 3) -> bool:
    return not MARKET.exists() or (datetime.now().timestamp() - MARKET.stat().st_mtime) > days * 86400


PACK_SCHEMA = {
    "type": "object",
    "properties": {
        "youtube_title": {"type": "string"},
        "youtube_description": {"type": "string"},
        "youtube_hashtags": {"type": "array", "items": {"type": "string"}},
        "youtube_tags": {"type": "array", "items": {"type": "string"}},
        "tiktok_caption": {"type": "string"},
        "tiktok_hashtags": {"type": "array", "items": {"type": "string"}},
        "instagram_caption": {"type": "string"},
        "instagram_hashtags": {"type": "array", "items": {"type": "string"}},
        "notes": {"type": "string"},
    },
    "required": ["youtube_title", "youtube_description", "youtube_hashtags", "youtube_tags", "tiktok_caption",
                 "tiktok_hashtags", "instagram_caption", "instagram_hashtags", "notes"],
    "additionalProperties": False,
}


COUNTDOWN = __import__("re").compile(
    r"\b(quedan?|faltan?|dentro de|en)\s+\d+\s*(d[ií]as?|horas?|semanas?)\b|\b\d+\s*d[ií]as?\b|\bhoy\b|"
    r"\bmañana\b|\bayer\b|\besta (semana|noche)\b|\b(days?|hours?) (left|to go)\b", 2)


def stale_text(pack: dict) -> list[str]:
    """Time-relative phrases that go out of date between writing and posting."""
    hits = []
    for k, v in pack.items():
        for x in (v if isinstance(v, list) else [v]):
            if isinstance(x, str) and (m := COUNTDOWN.search(x)):
                hits.append(f"{k}: '{m.group(0)}'")
    return hits


def package(clip_id: int) -> dict:
    """Write the title, description and hashtags for each platform before the clip is published."""
    with db.connect() as conn:
        c = dict(conn.execute("SELECT c.*, s.creator_id FROM clips c JOIN streams s ON s.id=c.stream_id "
                              "WHERE c.id=?", (clip_id,)).fetchone())
        posted_at = conn.execute("SELECT slot_at FROM clips WHERE id=?", (clip_id,)).fetchone()[0]
    from ..config import load_creators
    creator = next(x for x in load_creators() if x.id == c["creator_id"])
    edit = json.loads(c["edit_json"] or "{}")
    words = " ".join(w["w"] for w in json.loads(c["words_json"] or "[]"))
    market = MARKET.read_text() if MARKET.exists() else "(no market research yet)"
    r = llm.ask_json(SYSTEM + brief("growth"), (
        f"Package this short for publishing. It posts around {posted_at or 'soon'} (UTC); make sure nothing in the "
        f"text is out of date by then (no countdowns like 'quedan X días' unless still true).\n\n"
        f"Creator: {creator.display_name} (credit line is added automatically; still name the creator naturally "
        f"in the title). Language: {creator.language}.\n"
        f"Selector's title: {c['title']}\nHook on screen: {edit.get('hook')}\nWhy it was picked: {c['reason']}\n"
        f"What is said: {words[:2000]}\n\n# Market brief\n{market[:5000]}\n\n"
        "Write: youtube_title (<=90 chars, creator name + hook, Spanish, no clickbait lies, NO disclaimers like "
        "'clip no oficial' in titles: disclaimers go in descriptions/captions only), youtube_description "
        "(1-2 sentences with search keywords), youtube_hashtags (3, first ones show above the title), "
        "youtube_tags (8-15 search tags), tiktok_caption (1 short line + question/CTA, <=150 chars), "
        "tiktok_hashtags (3-5), instagram_caption (1-2 lines + CTA), instagram_hashtags (3-6). Hashtags without "
        "spaces. Never claim to be official. notes: one line on the strategy used."),
        PACK_SCHEMA, effort="low")
    if hits := stale_text(r):  # one rewrite without time-relative wording
        print(f"    #{clip_id} packaging had dated wording ({'; '.join(hits)}); rewriting")
        r = llm.ask_json(SYSTEM + brief("growth"), (
            "Rewrite this packaging so NOTHING is time-relative (no 'quedan X días', 'hoy', 'mañana', dates, "
            "countdowns): it may be read days later. Keep everything else.\n" + json.dumps(r, ensure_ascii=False)),
            PACK_SCHEMA, effort="low")
    r["youtube_title"] = __import__("re").sub(r"\s*[\(\[]?\s*clip(s)? no oficial(es)?\s*[\)\]]?", "",
                                               r["youtube_title"], flags=2).strip()
    edit["packaging"] = {**r, "written_at": datetime.now(timezone.utc).isoformat(), "dated_wording": stale_text(r)}
    with db.connect() as conn:
        conn.execute("UPDATE clips SET edit_json=? WHERE id=?", (json.dumps(edit, ensure_ascii=False), clip_id))
    return r
