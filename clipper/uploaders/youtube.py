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

SCOPES = ["https://www.googleapis.com/auth/youtube.upload"]
CLIENT = SECRETS / "youtube_client_secret.json"
TOKEN = SECRETS / "youtube_token.json"


def auth() -> None:
    if not CLIENT.exists():
        raise SystemExit(f"Missing {CLIENT}. See the docstring in clipper/uploaders/youtube.py.")
    creds = InstalledAppFlow.from_client_secrets_file(str(CLIENT), SCOPES).run_local_server(port=0)
    TOKEN.write_text(creds.to_json())
    print(f"Saved {TOKEN}")


def _creds() -> Credentials:
    if not TOKEN.exists():
        raise RuntimeError("Not authorised. Run `clipper auth youtube`.")
    creds = Credentials.from_authorized_user_file(str(TOKEN), SCOPES)
    if not creds.valid and creds.refresh_token:
        creds.refresh(Request())
        TOKEN.write_text(creds.to_json())
    return creds


def upload(path: Path, title: str, description: str, publish_at: datetime | None) -> str:
    cfg = SETTINGS["platforms"]["youtube"]
    yt = build("youtube", "v3", credentials=_creds(), cache_discovery=False)
    if "#shorts" not in (title + description).lower():
        description = f"{description}\n#Shorts"
    status = {"selfDeclaredMadeForKids": cfg.get("made_for_kids", False)}
    if publish_at:
        status.update(privacyStatus="private", publishAt=publish_at.strftime("%Y-%m-%dT%H:%M:%SZ"))
    else:
        status["privacyStatus"] = "public"
    body = {
        "snippet": {
            "title": title[:100],
            "description": description[:5000],
            "tags": cfg.get("default_tags", []),
            "categoryId": cfg.get("category_id", "20"),
        },
        "status": status,
    }
    req = yt.videos().insert(part="snippet,status", body=body,
                             media_body=MediaFileUpload(str(path), mimetype="video/mp4", resumable=True))
    resp = None
    while resp is None:
        _, resp = req.next_chunk()
    return resp["id"]
