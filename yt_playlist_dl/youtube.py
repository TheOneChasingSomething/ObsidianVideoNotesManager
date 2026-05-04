"""
youtube.py — Client YouTube Data API v3 (v2).

Enrichissement des VideoEntry avec channel, date, description, tags.
Détection des vidéos supprimées/privées via is_available.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from google.oauth2.credentials import Credentials

from .logger import get_logger

logger = get_logger(__name__)

_BATCH_SIZE = 50


@dataclass
class Playlist:
    id: str
    title: str
    description: str
    video_count: int
    thumbnail_url: str = ""

    def __str__(self) -> str:
        return f"{self.title} ({self.video_count} vidéos)"


@dataclass
class VideoEntry:
    video_id: str
    title: str
    playlist_id: str
    position: int
    url: str = field(init=False)
    channel_name: str = ""
    published_at: str = ""
    description: str = ""
    keywords: list[str] = field(default_factory=list)
    is_available: bool = True

    def __post_init__(self) -> None:
        self.url = f"https://www.youtube.com/watch?v={self.video_id}"

    def published_date_short(self) -> str:
        return self.published_at[:10] if self.published_at else ""

    def __str__(self) -> str:
        return f"[{self.position + 1:03d}] {self.title}"


class YouTubeClient:
    def __init__(self, credentials: Credentials) -> None:
        self._service = build("youtube", "v3", credentials=credentials)
        logger.debug("Client YouTube Data API v3 initialisé")

    def get_playlists(self) -> list[Playlist]:
        playlists: list[Playlist] = []
        page_token: Optional[str] = None
        while True:
            try:
                response = self._service.playlists().list(
                    part="snippet,contentDetails",
                    mine=True,
                    maxResults=50,
                    pageToken=page_token,
                ).execute()
            except HttpError as e:
                logger.error(f"Erreur API YouTube (playlists) : {e.reason} [{e.status_code}]")
                raise
            for item in response.get("items", []):
                snippet = item["snippet"]
                playlists.append(Playlist(
                    id=item["id"],
                    title=snippet.get("title", "(sans titre)"),
                    description=snippet.get("description", ""),
                    video_count=item["contentDetails"]["itemCount"],
                    thumbnail_url=snippet.get("thumbnails", {}).get("medium", {}).get("url", ""),
                ))
            page_token = response.get("nextPageToken")
            if not page_token:
                break
        playlists.sort(key=lambda p: p.title.lower())
        logger.info(f"{len(playlists)} playlist(s) récupérée(s)")
        return playlists

    def get_playlist_videos(
        self, playlist_id: str, max_videos: Optional[int] = None
    ) -> list[VideoEntry]:
        videos: list[VideoEntry] = []
        page_token: Optional[str] = None
        while True:
            try:
                response = self._service.playlistItems().list(
                    part="snippet,contentDetails",
                    playlistId=playlist_id,
                    maxResults=50,
                    pageToken=page_token,
                ).execute()
            except HttpError as e:
                logger.error(f"Erreur API (playlistItems, id={playlist_id}) : {e.reason}")
                raise
            for item in response.get("items", []):
                snippet = item["snippet"]
                video_id = snippet["resourceId"]["videoId"]
                title = snippet.get("title", "")
                is_available = title not in ("Deleted video", "Private video")
                videos.append(VideoEntry(
                    video_id=video_id,
                    title=title,
                    playlist_id=playlist_id,
                    position=snippet.get("position", len(videos)),
                    is_available=is_available,
                ))
                if max_videos and len(videos) >= max_videos:
                    return sorted(videos, key=lambda v: v.position)
            page_token = response.get("nextPageToken")
            if not page_token:
                break
        logger.info(f"{len(videos)} vidéo(s) récupérée(s) (playlist {playlist_id})")
        return sorted(videos, key=lambda v: v.position)

    def get_video_details(self, videos: list[VideoEntry]) -> list[VideoEntry]:
        """Enrichit les VideoEntry avec channel, publication, description, keywords."""
        index: dict[str, VideoEntry] = {v.video_id: v for v in videos}
        ids = list(index.keys())
        for i in range(0, len(ids), _BATCH_SIZE):
            batch_ids = ids[i: i + _BATCH_SIZE]
            try:
                response = self._service.videos().list(
                    part="snippet",
                    id=",".join(batch_ids),
                    maxResults=_BATCH_SIZE,
                ).execute()
            except HttpError as e:
                logger.warning(f"Erreur enrichissement métadonnées : {e.reason}")
                continue
            for item in response.get("items", []):
                vid = item["id"]
                if vid not in index:
                    continue
                snippet = item["snippet"]
                entry = index[vid]
                entry.channel_name = snippet.get("channelTitle", "")
                entry.published_at = snippet.get("publishedAt", "")
                entry.description = snippet.get("description", "")
                entry.keywords = snippet.get("tags", [])
                # Mise à jour du titre si absent ou URL
                real_title = snippet.get("title", "")
                if real_title and (not entry.title or entry.title.startswith("http") or entry.title == entry.video_id):
                    entry.title = real_title
        logger.debug(f"Métadonnées enrichies pour {len(ids)} vidéo(s)")
        return videos
