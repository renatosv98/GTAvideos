"""YouTube Data API v3 upload.

Setup: Google Cloud Console -> new project -> enable "YouTube Data API v3" -> OAuth consent screen
-> Credentials -> OAuth client ID (Desktop app) -> download JSON to secrets/youtube_client_secret.json
-> run `clipper auth youtube`.

Note: videos uploaded by an unverified API project are locked to private. Apply for the
YouTube API Services audit ("API compliance audit") early, because it can take weeks.
"""
from __future__ import annotations

from datetime import datetime
from pathlib import Path

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload

from ..config import SECRETS, SETTINGS

SCOPES = [
    "https://www.googleapis.com/auth/youtube.upload",
    "https://www.googleapis.com/auth/youtube.readonly",  # to confirm which channel we're logged into
    "https://www.googleapis.com/auth/youtube.force-ssl",  # edit/unschedule our own videos (manager fixes)
]
EDIT_SCOPE = "https://www.googleapis.com/auth/youtube.force-ssl"
CLIENT = SECRETS / "youtube_client_secret.json"
TOKEN = SECRETS / "youtube_token.json"


def _authorised_channel(creds: Credentials) -> tuple[str, str]:
    yt = build("youtube", "v3", credentials=creds, cache_discovery=False)
    items = yt.channels().list(part="snippet", mine=True).execute().get("items", [])
    if not items:
        raise RuntimeError("This Google account has no YouTube channel.")
    return items[0]["id"], items[0]["snippet"]["title"]


def _check_channel(creds: Credentials) -> None:
    expected = SETTINGS["platforms"]["youtube"].get("channel_id")
    cid, title = _authorised_channel(creds)
    if expected and cid != expected:
        raise RuntimeError(
            f"Logged into channel '{title}' ({cid}) but settings.yaml expects {expected}. "
            "Run `clipper auth youtube` again and pick the right channel in the Google account chooser."
        )


def auth() -> None:
    if not CLIENT.exists():
        raise SystemExit(f"Missing {CLIENT}. See the docstring in clipper/uploaders/youtube.py.")
    creds = InstalledAppFlow.from_client_secrets_file(str(CLIENT), SCOPES).run_local_server(
        port=0, prompt="select_account consent")
    cid, title = _authorised_channel(creds)
    expected = SETTINGS["platforms"]["youtube"].get("channel_id")
    if expected and cid != expected:
        raise SystemExit(f"⛔ You picked '{title}' ({cid}), not {expected}. Run again and choose the right channel.")
    TOKEN.write_text(creds.to_json())
    print(f"✅ Authorised as '{title}' ({cid}). Saved {TOKEN}")


def _creds() -> Credentials:
    if not TOKEN.exists():
        raise RuntimeError("Not authorised. Run `clipper auth youtube`.")
    creds = Credentials.from_authorized_user_file(str(TOKEN))  # keep the scopes the owner granted
    if not creds.valid and creds.refresh_token:
        creds.refresh(Request())
        TOKEN.write_text(creds.to_json())
    return creds


def upload(path: Path, title: str, description: str, publish_at: datetime | None,
           tags: list[str] | None = None) -> str:
    cfg = SETTINGS["platforms"]["youtube"]
    creds = _creds()
    _check_channel(creds)
    yt = build("youtube", "v3", credentials=creds, cache_discovery=False)
    status = {"selfDeclaredMadeForKids": cfg.get("made_for_kids", False)}
    if publish_at:
        status.update(privacyStatus="private", publishAt=publish_at.strftime("%Y-%m-%dT%H:%M:%SZ"))
    else:
        status["privacyStatus"] = "public"
    body = {
        "snippet": {
            "title": title[:100],
            "description": description[:5000],
            "tags": list(dict.fromkeys([*cfg.get("default_tags", []), *(t.lstrip("#") for t in (tags or []))]))[:30],
            "categoryId": cfg.get("category_id", "20"),
            "defaultLanguage": cfg.get("language", "es"),
            "defaultAudioLanguage": cfg.get("language", "es"),
        },
        "status": status,
    }
    req = yt.videos().insert(part="snippet,status", body=body,
                             media_body=MediaFileUpload(str(path), mimetype="video/mp4", resumable=True))
    resp = None
    while resp is None:
        _, resp = req.next_chunk()
    return resp["id"]


def can_edit() -> bool:
    """True once the owner re-authorised with edit permission (`clipper auth youtube`)."""
    try:
        return EDIT_SCOPE in (_creds().scopes or [])
    except Exception:
        return False


def _api():
    if not can_edit():
        raise RuntimeError("YouTube edit permission missing: run `clipper auth youtube` once.")
    return build("youtube", "v3", credentials=_creds(), cache_discovery=False)


def update(video_id: str, title: str | None = None, description: str | None = None, tags: list[str] | None = None,
           publish_at: datetime | None = None, private: bool = False) -> None:
    """Change text and/or schedule of one of our videos. private=True removes it from the schedule."""
    yt = _api()
    v = yt.videos().list(part="snippet,status", id=video_id).execute()["items"][0]
    sn, st = v["snippet"], v["status"]
    if title:
        sn["title"] = title[:100]
    if description:
        sn["description"] = description[:5000]
    if tags is not None:
        cfg = SETTINGS["platforms"]["youtube"]
        sn["tags"] = list(dict.fromkeys([*cfg.get("default_tags", []), *(t.lstrip("#") for t in tags)]))[:30]
    sn["defaultLanguage"] = sn["defaultAudioLanguage"] = SETTINGS["platforms"]["youtube"].get("language", "es")
    body = {"id": video_id, "snippet": {k: sn[k] for k in ("title", "description", "tags", "categoryId",
                                                           "defaultLanguage", "defaultAudioLanguage") if k in sn},
            "status": {"privacyStatus": st["privacyStatus"],
                       "selfDeclaredMadeForKids": st.get("selfDeclaredMadeForKids", False)}}
    if private:
        body["status"]["privacyStatus"] = "private"
    elif publish_at and st["privacyStatus"] == "private":
        body["status"]["publishAt"] = publish_at.strftime("%Y-%m-%dT%H:%M:%SZ")
    elif st.get("publishAt"):
        body["status"]["publishAt"] = st["publishAt"]
    yt.videos().update(part="snippet,status", body=body).execute()
