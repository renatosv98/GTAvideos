"""TikTok Content Posting API.

Setup: developers.tiktok.com -> create app -> add "Login Kit" + "Content Posting API" products,
scopes video.publish (direct) and/or video.upload (inbox/drafts), register a redirect URI,
put the keys in .env, then run `clipper auth tiktok`.

Until TikTok audits your app, direct posts are forced to private (SELF_ONLY). Use
platforms.tiktok.mode: inbox meanwhile: clips land in your TikTok drafts and you tap Post.
"""
from __future__ import annotations

import json
import os
import secrets as pysecrets
import time
import urllib.parse
from pathlib import Path

import requests

from ..config import SECRETS, SETTINGS

API = "https://open.tiktokapis.com/v2"
TOKEN = SECRETS / "tiktok_token.json"


def _env(k: str) -> str:
    v = os.environ.get(k)
    if not v:
        raise RuntimeError(f"{k} missing from .env")
    return v


def _save(tok: dict) -> None:
    tok["obtained_at"] = int(time.time())
    TOKEN.write_text(json.dumps(tok, indent=2))


def auth() -> None:
    state = pysecrets.token_urlsafe(16)
    scope = "user.info.basic,user.info.profile,video.publish,video.upload"
    url = "https://www.tiktok.com/v2/auth/authorize/?" + urllib.parse.urlencode({
        "client_key": _env("TIKTOK_CLIENT_KEY"), "scope": scope, "response_type": "code",
        "redirect_uri": _env("TIKTOK_REDIRECT_URI"), "state": state,
    })
    print("Open this URL, approve, then paste the FULL URL you were redirected to:\n\n" + url + "\n")
    redirected = input("Redirected URL: ").strip()
    q = urllib.parse.parse_qs(urllib.parse.urlparse(redirected).query)
    if "error" in q:
        raise SystemExit(f"TikTok refused the login: {q['error'][0]} - {q.get('error_description', [''])[0]}")
    if "code" not in q:
        raise SystemExit("That URL has no ?code=... in it. Copy the whole URL from the box on the callback page.")
    if q.get("state", [""])[0] != state:
        raise SystemExit("State mismatch. Try again.")
    r = requests.post(f"{API}/oauth/token/", data={
        "client_key": _env("TIKTOK_CLIENT_KEY"), "client_secret": _env("TIKTOK_CLIENT_SECRET"),
        "code": q["code"][0], "grant_type": "authorization_code", "redirect_uri": _env("TIKTOK_REDIRECT_URI"),
    }, timeout=30)
    tok = r.json()
    if "access_token" not in tok:
        raise SystemExit(f"TikTok login failed: {tok}")
    username = _username(tok["access_token"])
    expected = SETTINGS["platforms"]["tiktok"].get("username")
    if expected and username.lower() != expected.lower():
        raise SystemExit(f"⛔ You logged in as @{username}, not @{expected}. Log out in the browser and run again.")
    _save(tok)
    print(f"✅ Authorised as @{username}. Saved {TOKEN}")


def _username(token: str) -> str:
    r = requests.get(f"{API}/user/info/", params={"fields": "open_id,username,display_name"},
                     headers={"Authorization": f"Bearer {token}"}, timeout=30)
    data = r.json()
    if data.get("error", {}).get("code", "ok") != "ok":
        raise RuntimeError(f"TikTok user info: {data.get('error')}")
    return data["data"]["user"].get("username", "")


def _token() -> str:
    if not TOKEN.exists():
        raise RuntimeError("Not authorised. Run `clipper auth tiktok`.")
    tok = json.loads(TOKEN.read_text())
    if time.time() > tok["obtained_at"] + tok.get("expires_in", 0) - 300:
        r = requests.post(f"{API}/oauth/token/", data={
            "client_key": _env("TIKTOK_CLIENT_KEY"), "client_secret": _env("TIKTOK_CLIENT_SECRET"),
            "grant_type": "refresh_token", "refresh_token": tok["refresh_token"],
        }, timeout=30)
        r.raise_for_status()
        tok = r.json()
        _save(tok)
    return tok["access_token"]


def _post(path: str, token: str, body: dict) -> dict:
    r = requests.post(f"{API}{path}", headers={"Authorization": f"Bearer {token}",
                                               "Content-Type": "application/json; charset=UTF-8"},
                      json=body, timeout=60)
    data = r.json()
    if r.status_code >= 400 or data.get("error", {}).get("code", "ok") != "ok":
        raise RuntimeError(f"TikTok {path}: {data.get('error')}")
    return data["data"]


def upload(path: Path, title: str) -> str:
    cfg = SETTINGS["platforms"]["tiktok"]
    token = _token()
    expected = cfg.get("username")
    if expected and (u := _username(token)).lower() != expected.lower():
        raise RuntimeError(f"Authorised TikTok account is @{u}, expected @{expected}. Run `clipper auth tiktok`.")
    size = path.stat().st_size
    source = {"source": "FILE_UPLOAD", "video_size": size, "chunk_size": size, "total_chunk_count": 1}

    if cfg.get("mode", "direct") == "inbox":
        data = _post("/post/publish/inbox/video/init/", token, {"source_info": source})
    else:
        info = _post("/post/publish/creator_info/query/", token, {})
        privacy = cfg.get("privacy_level", "PUBLIC_TO_EVERYONE")
        if privacy not in info.get("privacy_level_options", []):
            privacy = "SELF_ONLY"
            print("   TikTok: requested privacy not allowed (app not audited?); posting as SELF_ONLY")
        data = _post("/post/publish/video/init/", token, {
            "post_info": {"title": title[:2200], "privacy_level": privacy,
                          "disable_duet": False, "disable_comment": False, "disable_stitch": False},
            "source_info": source,
        })

    with open(path, "rb") as f:
        r = requests.put(data["upload_url"], data=f, timeout=600, headers={
            "Content-Type": "video/mp4", "Content-Length": str(size),
            "Content-Range": f"bytes 0-{size - 1}/{size}",
        })
    r.raise_for_status()

    publish_id = data["publish_id"]
    for _ in range(60):
        st = _post("/post/publish/status/fetch/", token, {"publish_id": publish_id})
        if st.get("status") in ("PUBLISH_COMPLETE", "SEND_TO_USER_INBOX"):
            return publish_id
        if st.get("status") == "FAILED":
            raise RuntimeError(f"TikTok publish failed: {st.get('fail_reason')}")
        time.sleep(5)
    return publish_id  # still processing; TikTok will finish it
