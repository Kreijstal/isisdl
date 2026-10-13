from __future__ import annotations

import asyncio

from sqlalchemy.orm import Session as DatabaseSession

from isisdl.api.endpoints import VideoListAPI, DocumentListAPI, AssignmentAPI
from isisdl.api.models import MediaContainer, MediaURL, AuthenticatedSession, Course
from isisdl.backend.models import Config

__all__ = ["download_media_urls"]


async def gather_media_urls(db: DatabaseSession, session: AuthenticatedSession, courses: list[Course], config: Config) -> list[MediaURL]:
    requests = [DocumentListAPI.get(db, session, courses, config), AssignmentAPI.get(db, session, courses)]
    if config.dl_download_videos:
        requests.append(VideoListAPI.get(db, session, courses))

    urls = []
    for response in asyncio.as_completed(requests):
        urls.extend(await response or [])

    return urls


async def download_media_urls(db: DatabaseSession, urls: list[MediaURL]) -> list[MediaContainer]:
    """
    1. Figure out download urls (without internet, just based on the URL)
    2.

    - How to find out the names of all files?
      - Use the mimetype as a file extension hint

    - Figure out which containers need downloading
    - Every container that should be downloaded, create the file
    - Figure out the order of downloading for the containers
    - Resolve all conflicts in file paths
    - Filter same download url


    - Consistent and deterministic conflict resolution?
      - Hashing

    - After downloading everything, run the hardlink resolution once more, this time based on checksums.

    - *don't* download HTML
    """

    return []
