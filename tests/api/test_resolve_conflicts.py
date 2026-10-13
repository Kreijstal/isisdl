from datetime import datetime

from isisdl.api.download_urls import Download, resolve_conflicts
from isisdl.api.models import MediaType, MediaURL


def make(url: str, name: str, size: int, day: int, original_name: str | None = None) -> Download:
    media_url = MediaURL(url=url, course_id=1, media_type=MediaType.video, relative_path="")
    return Download(media_url, "Course", url, MediaType.video, "Videos", name, size, datetime(2020, 11, day), original_name=original_name)


def test_reused_suffixes_are_not_given_twice() -> None:
    # Two of the four videos called `Teil 1.mp4` were downloaded before and come from their MediaContainers with their suffix.
    # The other two are resolved again and must not take the suffixes `.0` and `.3`, or they would overwrite those files.
    reused = [make("a", "Teil 1.0.mp4", 100, 1, "Teil 1.mp4"), make("d", "Teil 1.3.mp4", 400, 4, "Teil 1.mp4")]
    resolved = [make("b", "Teil 1.mp4", 200, 2), make("c", "Teil 1.mp4", 300, 3)]

    names = {it.media_url.url: it.name for it in resolve_conflicts(reused + resolved)}

    assert names == {"a": "Teil 1.0.mp4", "b": "Teil 1.1.mp4", "c": "Teil 1.2.mp4", "d": "Teil 1.3.mp4"}


def test_suffixes_follow_time() -> None:
    downloads = [make("b", "Teil 1.mp4", 200, 2), make("a", "Teil 1.mp4", 100, 1)]

    names = {it.media_url.url: it.name for it in resolve_conflicts(downloads)}

    assert names == {"a": "Teil 1.0.mp4", "b": "Teil 1.1.mp4"}


def test_single_sibling_of_reused_file_is_suffixed() -> None:
    downloads = [make("a", "VL01.0.mp4", 100, 1, "VL01.mp4"), make("b", "VL01.mp4", 200, 2)]

    names = {it.media_url.url: it.name for it in resolve_conflicts(downloads)}

    assert names == {"a": "VL01.0.mp4", "b": "VL01.1.mp4"}


def test_reused_file_keeps_its_name() -> None:
    # A file with the same name was added after the first one was downloaded: The first one is not renamed.
    downloads = [make("a", "VL01.mp4", 100, 1, "VL01.mp4"), make("b", "VL01.mp4", 200, 2)]

    names = {it.media_url.url: it.name for it in resolve_conflicts(downloads)}

    assert names == {"a": "VL01.mp4", "b": "VL01.0.mp4"}
