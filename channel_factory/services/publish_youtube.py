from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from channel_factory.config import Profile, env_bool


class YouTubePublishError(RuntimeError):
    pass


YOUTUBE_UPLOAD_SCOPE = "https://www.googleapis.com/auth/youtube.upload"


def build_request_body(script: dict[str, Any], profile: Profile) -> dict[str, Any]:
    return {
        "snippet": {
            "title": script["title"],
            "description": script["summary"],
            "tags": list(script.get("tags", [])),
            "categoryId": "27",
        },
        "status": {
            "privacyStatus": "private",
            "selfDeclaredMadeForKids": profile.made_for_kids,
            "containsSyntheticMedia": bool(script.get("disclosure_required")),
        },
    }


def upload_private_video(
    video_path: Path,
    script: dict[str, Any],
    profile: Profile,
    client_secrets_path: Path,
) -> str:
    if not env_bool("CHANNEL_FACTORY_YOUTUBE_UPLOAD_ENABLED"):
        raise YouTubePublishError("YouTube API upload is disabled; use the baseline manual export")
    if not video_path.is_file() or not client_secrets_path.is_file():
        raise YouTubePublishError("Video and OAuth client-secrets files must exist")
    try:
        from googleapiclient.discovery import build
        from googleapiclient.http import MediaFileUpload
        from google_auth_oauthlib.flow import InstalledAppFlow
    except ImportError as exc:
        raise YouTubePublishError('Install the optional dependencies with pip install -e ".[youtube]"') from exc
    flow = InstalledAppFlow.from_client_secrets_file(str(client_secrets_path), [YOUTUBE_UPLOAD_SCOPE])
    credentials = flow.run_local_server(port=0)
    youtube = build("youtube", "v3", credentials=credentials)
    request = youtube.videos().insert(
        part="snippet,status",
        body=build_request_body(script, profile),
        media_body=MediaFileUpload(str(video_path), chunksize=4 * 1024 * 1024, resumable=True),
    )
    response: dict[str, Any] | None = None
    while response is None:
        _, response = request.next_chunk()
    video_id = response.get("id")
    if not video_id:
        raise YouTubePublishError("YouTube response did not include a video ID")
    return str(video_id)

