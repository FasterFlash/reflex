"""Open a Takeout ZIP safely and pull out the three files we use.

Everything stays in memory. Nothing is extracted to disk. The archive is treated as
hostile input: size, file-count and compression-ratio caps stop zip bombs, and any
member name that tries to escape the archive is refused.
"""
from __future__ import annotations

import io
import re
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO

from .errors import HtmlExport, NoYouTubeData, NotAZip, TooBig

MAX_ZIP_BYTES = 300 * 1024 * 1024          # same limit the upload screen enforces
MAX_UNCOMPRESSED = 1536 * 1024 * 1024      # all members together
MAX_MEMBER = 512 * 1024 * 1024             # one member
MAX_FILES = 20_000
MAX_RATIO = 200                            # uncompressed / compressed, per member

# Matched by position inside the archive, not by the leading folder name, which
# Google localises ("YouTube and YouTube Music" is English only).
_WATCH_JSON = re.compile(r"history/watch-history\.json$", re.I)
_WATCH_HTML = re.compile(r"history/watch-history\.html$", re.I)
_SEARCH_JSON = re.compile(r"history/search-history\.json$", re.I)
_SUBS_CSV = re.compile(r"subscriptions/subscriptions\.csv$", re.I)


@dataclass
class TakeoutFiles:
    watch: bytes
    search: bytes | None
    subscriptions: bytes | None
    member_count: int


def _unsafe(name: str) -> bool:
    n = name.replace("\\", "/")
    return n.startswith("/") or ".." in n.split("/") or re.match(r"^[A-Za-z]:", n) is not None


def open_takeout(src: Path | str | bytes | BinaryIO) -> TakeoutFiles:
    if isinstance(src, (str, Path)):
        size = Path(src).stat().st_size
        handle: BinaryIO = open(src, "rb")
    elif isinstance(src, (bytes, bytearray)):
        size = len(src)
        handle = io.BytesIO(src)
    else:
        handle = src
        handle.seek(0, io.SEEK_END)
        size = handle.tell()
        handle.seek(0)

    if size > MAX_ZIP_BYTES:
        raise TooBig(f"{size} bytes")

    try:
        zf = zipfile.ZipFile(handle)
    except zipfile.BadZipFile as exc:
        raise NotAZip(str(exc)) from exc

    with zf:
        infos = zf.infolist()
        if len(infos) > MAX_FILES:
            raise NotAZip("too many files in archive")
        total = 0
        for info in infos:
            if _unsafe(info.filename):
                raise NotAZip(f"unsafe member name: {info.filename!r}")
            total += info.file_size
            if info.compress_size and info.file_size / info.compress_size > MAX_RATIO and info.file_size > 10 * 1024 * 1024:
                raise NotAZip(f"suspicious compression ratio: {info.filename!r}")
        if total > MAX_UNCOMPRESSED:
            raise TooBig("archive expands past the limit")

        by_name = {i.filename: i for i in infos if not i.is_dir()}

        def find(pattern: re.Pattern[str]) -> zipfile.ZipInfo | None:
            hits = sorted(n for n in by_name if pattern.search(n))
            return by_name[hits[0]] if hits else None

        def read(info: zipfile.ZipInfo) -> bytes:
            with zf.open(info) as f:
                data = f.read(MAX_MEMBER + 1)
            if len(data) > MAX_MEMBER:
                raise TooBig(info.filename)
            return data

        watch = find(_WATCH_JSON)
        if watch is None:
            if find(_WATCH_HTML) is not None:
                raise HtmlExport("history is HTML, need JSON")
            raise NoYouTubeData("no watch-history file in archive")

        search = find(_SEARCH_JSON)
        subs = find(_SUBS_CSV)
        return TakeoutFiles(
            watch=read(watch),
            search=read(search) if search else None,
            subscriptions=read(subs) if subs else None,
            member_count=len(by_name),
        )
