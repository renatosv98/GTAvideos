"""Instagram Reels via the Instagram API with Instagram Login (no Facebook Page needed).

Setup: developers.facebook.com -> create app (Business) -> add the "Instagram" product ->
"API setup with Instagram login" -> Business login settings: add the redirect URI below ->
App roles -> Instagram testers: add your account and accept the invite in Instagram ->
put INSTAGRAM_APP_ID / INSTAGRAM_APP_SECRET / INSTAGRAM_REDIRECT_URI in .env -> `clipper auth instagram`.

While the app is in development mode it can only post to accounts with a role on the app,
which is exactly our case (our own account), so no Meta app review is needed.
Long-lived tokens last 60 days; this module refreshes them automatically when older than 7 days.

Newer Meta apps only get "Unified login": then put a system-user token (never expires) in .env as
INSTAGRAM_ACCESS_TOKEN plus INSTAGRAM_USER_ID (the Instagram account ID), and no `clipper auth` is needed.
"""
from __future__ import annotations

import json
import os
import secrets as pysecrets
import shutil
import subprocess
import tempfile
import time
import urllib.parse
from pathlib import Path

import requests

from ..config import SECRETS, SETTINGS

GRAPH = "https://graph.instagram.com"
TOKEN = SECRETS / "instagram_token.json"
SCOPES = "instagram_business_basic,instagram_business_content_publish"


def _cfg() -> dict:
    return SETTINGS["platforms"].get("instagram", {})


def _v() -> str:
    return _cfg().get("api_version", "v27.0")


def _env(k: str) -> str:
    v = os.environ.get(k)
    if not v:
        raise RuntimeError(f"{k} missing from .env")
    return v


def _check(r: requests.Response, what: str) -> dict:
    try:
        data = r.json()
    except ValueError:
        data = {"raw": r.text[:300]}
    if r.status_code >= 400 or "error" in data:
        raise RuntimeError(f"Instagram {what}: {data.get('error', data)}")
    return data


def _save(tok: dict) -> None:
    tok["obtained_at"] = int(time.time())
    TOKEN.write_text(json.dumps(tok, indent=2))
    TOKEN.chmod(0o600)


def _me(token: str) -> dict:
    return _check(requests.get(f"{GRAPH}/{_v()}/me", params={"fields": "user_id,username", "access_token": token},
                               timeout=30), "account info")


def auth() -> None:
    state = pysecrets.token_urlsafe(16)
    url = "https://www.instagram.com/oauth/authorize?" + urllib.parse.urlencode({
        "client_id": _env("INSTAGRAM_APP_ID"), "redirect_uri": _env("INSTAGRAM_REDIRECT_URI"),
        "response_type": "code", "scope": SCOPES, "state": state, "enable_fb_login": "0", "force_authentication": "1",
    })
    print("Open this URL, log in as your Instagram account, allow, then paste the FULL URL you were redirected to:\n\n"
          + url + "\n")
    redirected = input("Redirected URL: ").strip()
    q = urllib.parse.parse_qs(urllib.parse.urlparse(redirected).query)
    if "error" in q:
        raise SystemExit(f"Instagram refused the login: {q['error'][0]} - {q.get('error_description', [''])[0]}")
    if "code" not in q:
        raise SystemExit("That URL has no ?code=... in it. Copy the whole URL from the box on the callback page.")
    if q.get("state", [""])[0] != state:
        raise SystemExit("State mismatch. Try again.")
    code = q["code"][0].removesuffix("#_")

    short = _check(requests.post("https://api.instagram.com/oauth/access_token", data={
        "client_id": _env("INSTAGRAM_APP_ID"), "client_secret": _env("INSTAGRAM_APP_SECRET"),
        "grant_type": "authorization_code", "redirect_uri": _env("INSTAGRAM_REDIRECT_URI"), "code": code,
    }, timeout=30), "login")
    long = _check(requests.get(f"{GRAPH}/access_token", params={
        "grant_type": "ig_exchange_token", "client_secret": _env("INSTAGRAM_APP_SECRET"),
        "access_token": short["access_token"],
    }, timeout=30), "long-lived token")

    me = _me(long["access_token"])
    expected = _cfg().get("username")
    if expected and me.get("username", "").lower() != expected.lower():
        raise SystemExit(f"⛔ You logged in as @{me.get('username')}, not @{expected}. Log out on instagram.com and retry.")
    _save({**long, "user_id": str(me["user_id"]), "username": me.get("username")})
    print(f"✅ Authorised as @{me.get('username')}. Saved {TOKEN}")


def _system_user() -> bool:
    """A Facebook-type token in .env (system user, never expires) -> graph.facebook.com.
    Instagram-type tokens (start with "IG", from the app dashboard's "Generate token") go through the normal
    token file instead, so they get refreshed before their 60-day expiry."""
    t = os.environ.get("INSTAGRAM_ACCESS_TOKEN", "")
    return bool(t) and not t.startswith("IG")


def _import_dashboard_token() -> None:
    """First use of an Instagram token pasted into .env: store it in the token file (that's where refreshes go)."""
    t = os.environ.get("INSTAGRAM_ACCESS_TOKEN", "")
    if not t.startswith("IG"):
        return
    old = json.loads(TOKEN.read_text()) if TOKEN.exists() else {}
    if old.get("imported") == t[-12:]:
        return  # already imported (maybe refreshed since)
    me = _me(t)
    _save({"access_token": t, "expires_in": 60 * 86400, "user_id": str(me.get("user_id") or _env("INSTAGRAM_USER_ID")),
           "username": me.get("username", ""), "imported": t[-12:]})


def _host() -> str:
    return "https://graph.facebook.com" if _system_user() else GRAPH


def _token() -> dict:
    if _system_user():
        token, uid = _env("INSTAGRAM_ACCESS_TOKEN"), _env("INSTAGRAM_USER_ID")
        me = _check(requests.get(f"{_host()}/{_v()}/{uid}", params={"fields": "username", "access_token": token},
                                 timeout=30), "account info")
        return {"access_token": token, "user_id": uid, "username": me.get("username", "")}
    _import_dashboard_token()
    if not TOKEN.exists():
        raise RuntimeError("Not authorised. Run `clipper auth instagram`.")
    tok = json.loads(TOKEN.read_text())
    age = time.time() - tok["obtained_at"]
    if age > tok.get("expires_in", 0) - 3600:
        raise RuntimeError("Instagram token expired. Run `clipper auth instagram`.")
    if age > 7 * 86400:  # refresh well before the 60-day expiry
        new = _check(requests.get(f"{GRAPH}/refresh_access_token", params={
            "grant_type": "ig_refresh_token", "access_token": tok["access_token"]}, timeout=30), "token refresh")
        tok.update(new)
        _save(tok)
    return tok


def upload(path: Path, caption: str) -> str:
    """Publish a Reel; returns the media id."""
    tok = _token()
    token, uid = tok["access_token"], tok["user_id"]
    expected = _cfg().get("username")
    if expected and tok.get("username", "").lower() != expected.lower():
        raise RuntimeError(f"Authorised Instagram account is @{tok.get('username')}, expected @{expected}.")

    if _cfg().get("upload_method", "github") == "github":
        # Instagram fetches the video from a public link: stage it as a temporary GitHub release file
        name, url = _stage(path)
        try:
            cid = _create_and_wait(uid, token, caption, video_url=url)
        finally:
            _unstage(name)
    else:
        cid = _create_and_wait(uid, token, caption, path=path)

    pub = _check(requests.post(f"{_host()}/{_v()}/{uid}/media_publish",
                               data={"creation_id": cid, "access_token": token}, timeout=60), "publish")
    return pub["id"]


def _gh(*args: str) -> str:
    gh = shutil.which("gh") or "/opt/homebrew/bin/gh"
    r = subprocess.run([gh, *args], capture_output=True, text=True, timeout=600)
    if r.returncode:
        raise RuntimeError(f"gh {args[0]} {args[1]}: {r.stderr.strip()[:200]}")
    return r.stdout


def _stage(path: Path) -> tuple[str, str]:
    repo, tag = _cfg().get("staging_repo", "renatosv98/GTAvideos"), _cfg().get("staging_release", "ig-staging")
    name = f"reel-{pysecrets.token_hex(6)}.mp4"
    with tempfile.TemporaryDirectory() as d:
        staged = Path(d) / name
        shutil.copyfile(path, staged)
        _gh("release", "upload", tag, str(staged), "-R", repo, "--clobber")
    return name, f"https://github.com/{repo}/releases/download/{tag}/{name}"


def _unstage(name: str) -> None:
    try:
        _gh("release", "delete-asset", _cfg().get("staging_release", "ig-staging"), name,
            "-R", _cfg().get("staging_repo", "renatosv98/GTAvideos"), "-y")
    except Exception as e:
        print(f"   could not delete staged file {name}: {e}")


def _create_and_wait(uid: str, token: str, caption: str, video_url: str | None = None,
                     path: Path | None = None) -> str:
    data = {"media_type": "REELS", "caption": caption[:2200], "share_to_feed": "true", "access_token": token}
    if video_url:
        data["video_url"] = video_url
    else:
        data["upload_type"] = "resumable"
    container = _check(requests.post(f"{_host()}/{_v()}/{uid}/media", data=data, timeout=60), "create container")
    cid = container["id"]
    if path:
        size = path.stat().st_size
        with open(path, "rb") as f:
            _check(requests.post(container.get("uri") or f"https://rupload.facebook.com/ig-api-upload/{_v()}/{cid}",
                                 data=f, timeout=600,
                                 headers={"Authorization": f"OAuth {token}", "offset": "0", "file_size": str(size)}),
                   "video upload")

    for _ in range(120):  # Instagram transcodes the video; usually under a minute
        st = _check(requests.get(f"{_host()}/{_v()}/{cid}", params={"fields": "status_code,status",
                                                                   "access_token": token}, timeout=30), "status")
        if st.get("status_code") == "FINISHED":
            break
        if st.get("status_code") in ("ERROR", "EXPIRED"):
            raise RuntimeError(f"Instagram processing failed: {st.get('status')}")
        time.sleep(5)
    else:
        raise RuntimeError("Instagram is still processing the video after 10 min; try again later.")
    return cid


def permalink(media_id: str) -> str | None:
    try:
        tok = _token()
        return _check(requests.get(f"{_host()}/{_v()}/{media_id}", params={
            "fields": "permalink", "access_token": tok["access_token"]}, timeout=30), "permalink").get("permalink")
    except Exception:
        return None
