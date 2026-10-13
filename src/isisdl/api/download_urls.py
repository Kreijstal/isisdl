from __future__ import annotations

import asyncio
import os
import re
import sqlite3
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime
from email.utils import parsedate_to_datetime
from hashlib import sha256
from pathlib import Path
from typing import cast
from urllib.parse import urlparse

from sqlalchemy import select, delete
from sqlalchemy.orm import Session as DatabaseSession

from isisdl.api.endpoints import VideoListAPI, DocumentListAPI, AssignmentAPI
from isisdl.api.models import MediaContainer, MediaURL, AuthenticatedSession, Course, MediaType, Error
from isisdl.api.rate_limiter import RateLimiter, ThrottleType, Token
from isisdl.backend.models import Config
from isisdl.db_conf import add_or_update_objects_to_database
from isisdl.settings import discover_num_threads, perc_diff_for_checksum, intern_dir_location, legacy_database_file_location
from isisdl.utils import path, sanitize_name, calculate_local_checksum, parse_google_drive_url, get_url_from_gdrive_confirmation, HumanBytes, do_ffprobe, config as legacy_config

__all__ = ["download_media_urls", "sync_media_urls"]


async def gather_media_urls(db: DatabaseSession, session: AuthenticatedSession, courses: list[Course], config: Config) -> list[MediaURL]:
    requests = [DocumentListAPI.get(db, session, courses, config), AssignmentAPI.get(db, session, courses)]
    if config.dl_download_videos:
        requests.append(VideoListAPI.get(db, session, courses))

    urls = []
    for response in asyncio.as_completed(requests):
        urls.extend(await response or [])

    return urls


@dataclass
class Download:
    """
    A MediaURL with everything needed to download it: Where it comes from, where it goes and what it should look like.
    """
    media_url: MediaURL
    course_dir: str
    download_url: str
    media_type: MediaType
    relative_path: str
    name: str
    size: int
    time: datetime
    links: list[Download] = field(default_factory=list)

    @property
    def path(self) -> Path:
        return path(self.course_dir, self.relative_path, self.name)

    @property
    def throttle_type(self) -> ThrottleType:
        return ThrottleType.from_media_type(self.media_type)


# Like 1.3: Files without a directory of their own are sorted by their type
default_directories = {MediaType.video: "Videos", MediaType.extern: "Extern"}


def read_media_containers(db: DatabaseSession) -> dict[tuple[int, str], MediaContainer]:
    return {(it.course_id, it.url): it for it in db.execute(select(MediaContainer)).scalars().all()}


def course_directory(course: Course) -> str:
    # Courses can still only be renamed with the 1.3 config (`isisdl --config`). Its json keys are strings.
    renamed = legacy_config.renamed_courses or {}
    name = renamed.get(str(course.id)) or renamed.get(course.id)  # type: ignore[call-overload]

    return sanitize_name(name or course.preferred_name or course.short_name, True)


def relative_directory(media_url: MediaURL, config: Config) -> str:
    if not config.fs_make_subdirs:
        return ""

    return sanitize_name((media_url.relative_path or default_directories.get(media_url.media_type, "")).strip("/"), True)


def token_params(session: AuthenticatedSession, url: str) -> dict[str, str]:
    # The Moodle token authenticates `webservice/pluginfile.php`. It must not be handed out to other sites.
    if urlparse(url).hostname == "isis.tu-berlin.de":
        return {"token": session.api_token}

    return {}


def from_container(media_url: MediaURL, container: MediaContainer) -> Download:
    return Download(
        media_url, course_directory(media_url.course), container.download_url, container.media_type,
        container.relative_path, container.name, container.size, container.time_modified
    )


async def resolve_download_url(session: AuthenticatedSession, url: str) -> str | None:
    if "tubcloud.tu-berlin.de" in url:
        return url if url.endswith("/download") else url + "/download"

    if "https://drive.google.com/" in url:
        drive_id = parse_google_drive_url(url)
        if drive_id is None:
            return None

        download_url = f"https://drive.google.com/uc?id={drive_id}"
        async with session.get(download_url) as response:
            if isinstance(response, Error) or not response.ok:
                return None

            if "Content-Disposition" in response.headers:
                return download_url

            return get_url_from_gdrive_confirmation(await response.text())

    return url


def is_downloadable(content_type: str | None) -> bool:
    return content_type is not None and (content_type.startswith("application/") or content_type.startswith("video/"))


async def resolve(session: AuthenticatedSession, media_url: MediaURL, config: Config, semaphore: asyncio.Semaphore) -> Download:
    """
    Finds out name, size and time of a MediaURL. If they are not known from the API, the headers of the file are requested.
    Everything that is not a file (e.g. an HTML page) is marked as corrupted, which is not downloaded.
    """

    def make(download_url: str, media_type: MediaType, name: str, size: int, time: datetime) -> Download:
        return Download(media_url, course_directory(media_url.course), download_url, media_type, relative_directory(media_url, config), sanitize_name(name, False), size, time)

    if media_url.name is not None and media_url.size is not None and media_url.time_modified is not None:
        return make(media_url.url, media_url.media_type, media_url.name, media_url.size, media_url.time_modified)

    async with semaphore:
        try:
            download_url = await resolve_download_url(session, media_url.url)
        except Exception:
            download_url = None

        if download_url is None:
            return make(media_url.url, MediaType.corrupted, os.path.basename(media_url.url), 0, datetime.now())

        try:
            async with session.get(download_url, params=token_params(session, download_url)) as response:
                if isinstance(response, Error) or not response.ok or not is_downloadable(response.headers.get("Content-Type")):
                    return make(download_url, MediaType.corrupted, os.path.basename(media_url.url), 0, datetime.now())

                headers = response.headers

        except Exception:
            return make(download_url, MediaType.corrupted, os.path.basename(media_url.url), 0, datetime.now())

    if media_url.name is not None:
        name = media_url.name
    elif names := re.findall("filename=\"(.*?)\"", headers.get("Content-Disposition", "")):
        name = names[0]
    else:
        name = os.path.basename(media_url.url)

    size = media_url.size
    if size is None:
        if "Content-Length" not in headers:
            return make(download_url, MediaType.corrupted, name, 0, datetime.now())

        size = int(headers["Content-Length"])

    time = media_url.time_modified
    if time is None:
        try:
            time = parsedate_to_datetime(headers["Last-Modified"]).replace(tzinfo=None)
        except Exception:
            time = datetime.now()

    return make(download_url, media_url.media_type, name, size, time)


def resolve_conflicts(downloads: list[Download]) -> list[Download]:
    """
    The same as `check_for_conflicts_in_files` of 1.3, but deterministic:
      1. Files with the same name and size in a course are the same file. It is downloaded once and hardlinked to the other paths.
      2. Different files with the same path are renamed to `name.0.ext`, `name.1.ext`, … ordered by time.
      3. Files with the same download url are downloaded once and hardlinked to the other paths.
    The returned downloads are the ones that have to be downloaded, hardlinks are attached to them.
    """

    def order(it: Download) -> tuple[datetime, str]:
        return it.time, it.media_url.url

    same_file: defaultdict[tuple[int, str, int], list[Download]] = defaultdict(list)
    for it in downloads:
        same_file[(it.media_url.course_id, it.name, it.size)].append(it)

    final: list[Download] = []
    remaining: list[Download] = []
    for group in same_file.values():
        first, *others = sorted(group, key=order)
        first.links.extend(other for other in others if other.path != first.path)
        (remaining if not others else final).append(first)

    same_path: defaultdict[Path, list[Download]] = defaultdict(list)
    for it in remaining:
        same_path[it.path].append(it)

    renamed = []
    for group in same_path.values():
        if len(group) == 1:
            renamed.append(group[0])
            continue

        # 1.3 assigned the suffixes in random order for files with the same time. Keep the assignment of files that already exist.
        basename, ext = os.path.splitext(group[0].name)
        names = [sanitize_name(f"{basename}.{i}{ext}", False) for i in range(len(group))]
        remaining_files = sorted(group, key=order)
        assignment: dict[int, Download] = {}

        for i, name in enumerate(names):
            existing = group[0].path.parent.joinpath(name)
            if not existing.exists():
                continue

            match = next((it for it in remaining_files if it.size == existing.stat().st_size), None)
            if match is not None:
                assignment[i] = match
                remaining_files.remove(match)

        for i, it in zip([i for i in range(len(group)) if i not in assignment], remaining_files):
            assignment[i] = it

        for i, it in assignment.items():
            it.name = names[i]
            renamed.append(it)

    same_download_url: defaultdict[str, list[Download]] = defaultdict(list)
    for it in renamed:
        same_download_url[it.download_url].append(it)

    for group in same_download_url.values():
        first, *others = sorted(group, key=order)
        first.links.extend(other for other in others if other.path != first.path)
        final.append(first)

    return final


def adopt_existing_locations(downloads: list[Download], config: Config) -> None:
    """
    1.3 placed the files of folder modules directly in the course directory.
    If such a file already exists there with the expected size, it is used in place instead of downloading it again.
    """
    planned = {it.path for it in downloads}

    for it in downloads:
        if it.path.exists() or not config.fs_make_subdirs:
            continue

        legacy_directory = sanitize_name(default_directories.get(it.media_url.media_type, ""), True)
        legacy_path = path(it.course_dir, legacy_directory, it.name)

        if legacy_path not in planned and legacy_path.exists() and legacy_path.stat().st_size == it.size:
            it.relative_path = legacy_directory


def size_before_compression(file: Path) -> int | None:
    """
    `isisdl --compress` stores the size of the original video in the metadata of the compressed one.
    """
    probe = do_ffprobe(file)
    if probe is None:
        return None

    try:
        return int(str(probe["format"]["tags"]["previous_size"]).strip("\""))
    except (KeyError, ValueError):
        return None


def needs_download(it: Download, container: MediaContainer | None) -> bool:
    if not it.path.exists():
        return True

    actual_size = it.path.stat().st_size
    if actual_size == 0:
        return True

    if actual_size == it.size or (container is not None and container.size == actual_size):
        return False

    if it.media_type == MediaType.video:
        original_size = size_before_compression(it.path)
        if original_size is not None and (original_size == it.size or (container is not None and container.size == original_size)):
            return False

    if container is None or container.checksum is None:
        return True

    if it.size * (1 - perc_diff_for_checksum) <= actual_size <= it.size * (1 + perc_diff_for_checksum):
        return False

    return calculate_local_checksum(it.path) != container.checksum


async def download(session: AuthenticatedSession, it: Download, limiter: RateLimiter, semaphore: asyncio.Semaphore) -> bool:
    """
    Downloads into a temporary file, which only replaces the target once it is complete.
    """
    temp_dir = path(intern_dir_location, "downloads")
    os.makedirs(temp_dir, exist_ok=True)
    temp_file = temp_dir.joinpath(sha256(str(it.path).encode()).hexdigest())

    async with semaphore:
        limiter.register(it.throttle_type)
        try:
            async with session.get(it.download_url, params=token_params(session, it.download_url)) as response:
                if isinstance(response, Error) or not response.ok:
                    return False

                with temp_file.open("wb") as f:
                    while True:
                        token = await limiter.get(it.throttle_type)
                        chunk = await response.content.read(token.num_bytes)
                        limiter.return_token(Token(len(chunk)))

                        if not chunk:
                            break

                        f.write(chunk)

        except Exception:
            temp_file.unlink(missing_ok=True)
            return False

        finally:
            limiter.completed(it.throttle_type)

    it.path.parent.mkdir(parents=True, exist_ok=True)
    os.replace(temp_file, it.path)
    it.size = it.path.stat().st_size

    return True


def link(source: Download, target: Download) -> None:
    target.path.parent.mkdir(parents=True, exist_ok=True)
    target.path.unlink(missing_ok=True)
    os.link(source.path, target.path)


def read_legacy_checksums() -> dict[tuple[int, str], tuple[int, str]]:
    """
    The checksums that isisdl 1.3 calculated, by (course id, url). This saves reading every file again when migrating.
    """
    try:
        legacy = sqlite3.connect(f"file:{path(legacy_database_file_location)}?mode=ro", uri=True)
        try:
            rows = legacy.execute("SELECT course_id, url, size, checksum FROM fileinfo WHERE checksum IS NOT NULL").fetchall()
        finally:
            legacy.close()

    except sqlite3.Error:
        return {}

    return {(course_id, url): (size, checksum) for course_id, url, size, checksum in rows}


def store_containers(db: DatabaseSession, downloads: list[tuple[Download, str | None]]) -> list[MediaContainer] | None:
    existing = read_media_containers(db)

    return add_or_update_objects_to_database(
        db, existing, [
            {
                "url": it.media_url.url, "course_id": it.media_url.course_id, "download_url": it.download_url, "media_type": it.media_type,
                "relative_path": it.relative_path, "name": it.name, "size": it.size,
                "time_created": it.media_url.time_created or it.time, "time_modified": it.time, "checksum": checksum,
            }
            for it, checksum in downloads
        ],
        MediaContainer, lambda x: (cast(int, x["course_id"]), cast(str, x["url"])),
        {key: key for key in ["url", "course_id", "download_url", "media_type", "relative_path", "name", "size", "time_created", "time_modified", "checksum"]},
        {}
    )


async def download_media_urls(db: DatabaseSession, session: AuthenticatedSession, urls: list[MediaURL], config: Config, should_download: bool = True) -> list[MediaContainer]:
    """
    Downloads all files that are missing or changed and records them as MediaContainers.
    Files that already exist with the expected size (e.g. downloaded by isisdl 1.3) are not downloaded again.
    With `should_download=False`, only the files that exist are recorded and nothing on disk is changed.
    Returns the MediaContainers that were newly downloaded.
    """
    containers = read_media_containers(db)
    semaphore = asyncio.Semaphore(discover_num_threads)

    async def resolve_or_reuse(media_url: MediaURL) -> Download:
        container = containers.get((media_url.course_id, media_url.url))
        is_outdated = container is not None and media_url.time_modified is not None and container.time_modified < media_url.time_modified

        # ISIS should always serve its documents and videos (the video server is flaky though), so failures are tried again.
        # Other urls that were not downloadable are not requested again.
        is_retry = container is not None and container.media_type == MediaType.corrupted and media_url.media_type != MediaType.extern

        if container is not None and not is_outdated and not is_retry:
            return from_container(media_url, container)

        return await resolve(session, media_url, config, semaphore)

    resolved = await asyncio.gather(*(resolve_or_reuse(it) for it in urls))
    corrupted = [it for it in resolved if it.media_type == MediaType.corrupted]
    downloads = resolve_conflicts([it for it in resolved if it.media_type != MediaType.corrupted])
    adopt_existing_locations(downloads + [target for it in downloads for target in it.links], config)

    to_download = [it for it in downloads if needs_download(it, containers.get((it.media_url.course_id, it.media_url.url)))]
    print(f"Found {len(resolved)} files: {len(to_download)} {'to download' if should_download else 'missing or changed'} ({HumanBytes.format_str(sum(it.size for it in to_download))}), {len(corrupted)} are not downloadable.")

    if not should_download:
        missing = {id(it) for it in to_download}
        stored: list[tuple[Download, str | None]] = [(it, None) for it in corrupted]
        for it in downloads:
            for target in [it, *it.links]:
                if id(target) not in missing and target.path.exists():
                    stored.append((target, calculate_local_checksum(target.path)))

        store_containers(db, stored)
        return []

    limiter = RateLimiter.from_bandwidth(config.dl_throttle_rate) if config.dl_throttle_rate else RateLimiter(None)

    async def download_and_report(it: Download) -> bool:
        ok = await download(session, it, limiter, semaphore)
        print(f"{'Downloaded' if ok else 'Failed'}: {it.path if config.dl_show_full_path else it.name}")
        return ok

    results = await asyncio.gather(*(download_and_report(it) for it in to_download))
    await limiter.finish()

    newly_downloaded = {id(it) for it, ok in zip(to_download, results) if ok}
    failed = {id(it) for it, ok in zip(to_download, results) if not ok}
    legacy_checksums = read_legacy_checksums() if any((it.media_url.course_id, it.media_url.url) not in containers for it in downloads) else {}

    def checksum_of(it: Download) -> str | None:
        key = (it.media_url.course_id, it.media_url.url)
        container = containers.get(key)
        if id(it) not in newly_downloaded and container is not None and container.checksum is not None:
            return container.checksum

        if not it.path.exists():
            return None

        if id(it) not in newly_downloaded and key in legacy_checksums:
            size, checksum = legacy_checksums[key]
            if size == it.path.stat().st_size:
                return checksum

        return calculate_local_checksum(it.path)

    stored = [(it, None) for it in corrupted]
    for it in downloads:
        if id(it) in failed:
            continue

        checksum = checksum_of(it)
        stored.append((it, checksum))

        for target in it.links:
            if needs_download(target, containers.get((target.media_url.course_id, target.media_url.url))):
                link(it, target)

            stored.append((target, checksum))

    store_containers(db, stored)

    new_containers = read_media_containers(db)
    return [new_containers[(it.media_url.course_id, it.media_url.url)] for it in to_download if id(it) in newly_downloaded]


async def sync_media_urls(db: DatabaseSession, session: AuthenticatedSession, urls: list[MediaURL], config: Config) -> None:
    """
    Rebuilds the records of the downloaded files from what is on disk. Nothing is downloaded or deleted.
    Files that are missing or changed are downloaded on the next run.
    """
    db.execute(delete(MediaContainer))
    db.commit()

    await download_media_urls(db, session, urls, config, should_download=False)

    containers = read_media_containers(db).values()
    known = {path(course_directory(it.course), it.relative_path, it.name) for it in containers}
    course_dirs = {path(course_directory(it.course)) for it in urls}
    unknown = sorted(file for course_dir in course_dirs if course_dir.is_dir() for file in course_dir.rglob("*") if file.is_file() and file not in known)

    print(f"Recorded {sum(it.media_type != MediaType.corrupted for it in containers)} files that exist on disk.")
    if not unknown:
        return

    print(f"\n{len(unknown)} files in the course directories do not belong to any file on ISIS ({HumanBytes.format_str(sum(it.stat().st_size for it in unknown))}):")
    for file in unknown[:50]:
        print(f"  {file}")

    if len(unknown) > 50:
        print(f"  … and {len(unknown) - 50} more")
