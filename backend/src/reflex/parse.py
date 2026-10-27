"""Turn the raw Takeout files into clean tables, and say honestly what was thrown away.

Takeout lists events newest-first. We sort chronologically, converting to IST up front
so every later day/week/hour bucket uses the same clock.
"""
from __future__ import annotations

import csv
import io
import json
import re
import unicodedata
from collections import Counter
from dataclasses import dataclass, field
from functools import lru_cache
from datetime import datetime, timezone

import ftfy
import polars as pl

from .errors import NoYouTubeData
from .ingest import TakeoutFiles

TZ = "Asia/Kolkata"

_WS = re.compile(r"\s+")

WATCH_SCHEMA = {
    "src_idx": pl.Int64,
    "video_id": pl.String,
    "title": pl.String,
    "title_removed": pl.Boolean,
    "channel_id": pl.String,
    "channel_name": pl.String,
    "channel_key": pl.String,
    "url_is_short": pl.Boolean,
    "is_music": pl.Boolean,
    "ts_utc": pl.Datetime("us", "UTC"),
}
SEARCH_SCHEMA = {"src_idx": pl.Int64, "query": pl.String, "ts_utc": pl.Datetime("us", "UTC")}
SUBS_SCHEMA = {"channel_id": pl.String, "channel_url": pl.String, "channel_name": pl.String}


@lru_cache(maxsize=65536)
def _repair(value: str) -> str:
    # Mojibake can only exist where there are non-ASCII characters, so plain-ASCII
    # strings (most titles) skip the slow repair entirely.
    if value.isascii():
        return value
    return unicodedata.normalize("NFC", ftfy.fix_encoding(value))


def clean_text(value: object) -> str | None:
    """Repair mojibake, normalise unicode, collapse whitespace. Never rewrites quotes or width."""
    if not isinstance(value, str):
        return None
    s = _repair(value)
    s = _WS.sub(" ", s).strip()
    return s or None


def parse_time(value: object) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        dt = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


_HOST = r"https?://(?:www\.|m\.|music\.)?"
_WATCH_URL = re.compile(_HOST + r"youtube\.com/watch\?(?:[^#]*&)?v=([A-Za-z0-9_-]{11})(?:[&#]|$)")
_SHORTS_URL = re.compile(_HOST + r"youtube\.com/shorts/([A-Za-z0-9_-]{11})(?:[/?#]|$)")
_SHORT_LINK = re.compile(r"https?://youtu\.be/([A-Za-z0-9_-]{11})(?:[/?#]|$)")
_CHANNEL_URL = re.compile(_HOST + r"youtube\.com/channel/(UC[A-Za-z0-9_-]{22})/?$")


def video_id_from_url(url: object) -> tuple[str | None, bool]:
    """(video_id, is_shorts_url). Takeout nearly always gives watch?v=, but /shorts/ is handled.

    Regexes rather than urllib: this runs once per event and urlparse dominated the
    parse time on large histories.
    """
    if not isinstance(url, str):
        return None, False
    m = _WATCH_URL.match(url) or _SHORT_LINK.match(url)
    if m:
        return m.group(1), False
    m = _SHORTS_URL.match(url)
    if m:
        return m.group(1), True
    return None, False


def channel_id_from_url(url: object) -> str | None:
    if not isinstance(url, str):
        return None
    m = _CHANNEL_URL.match(url)
    return m.group(1) if m else None


@dataclass
class QualityReport:
    """What the file contained, and what we did to it. This is the data-messiness story."""

    watch_rows_in: int = 0
    watch_rows_kept: int = 0
    dropped: Counter = field(default_factory=Counter)
    music_rows: int = 0                  # YouTube Music plays: kept, flagged, excluded from video stats
    removed_title_rows: int = 0          # video deleted/private: title is just a URL
    missing_channel_rows: int = 0        # no `subtitles`: channel unknowable
    channels_without_id: int = 0         # grouped by name instead
    same_timestamp_rows: int = 0         # events sharing an exact timestamp
    distinct_videos: int = 0
    distinct_channels: int = 0
    first_ist: str | None = None
    last_ist: str | None = None
    span_days: int = 0
    active_days: int = 0
    search_rows_in: int = 0
    search_rows_kept: int = 0
    search_dropped: Counter = field(default_factory=Counter)
    subscriptions: int | None = None     # None: file was not in the export
    members_in_zip: int = 0

    def to_dict(self) -> dict:
        d = dict(self.__dict__)
        d["dropped"] = dict(self.dropped)
        d["search_dropped"] = dict(self.search_dropped)
        return d

    def render(self) -> str:
        pct = lambda n: f"{n / self.watch_rows_kept:.1%}" if self.watch_rows_kept else "n/a"
        lines = [
            "WATCH HISTORY",
            f"  rows in file        {self.watch_rows_in:>9,}",
            f"  rows kept           {self.watch_rows_kept:>9,}",
            f"    of which music    {self.music_rows:>9,}  (flagged, left out of video stats)",
        ]
        for reason, n in self.dropped.most_common():
            lines.append(f"    dropped: {reason:<14}{n:>9,}")
        lines += [
            f"  range (IST)         {self.first_ist}  ->  {self.last_ist}",
            f"  span / active days  {self.span_days} / {self.active_days}",
            f"  distinct videos     {self.distinct_videos:>9,}  (excluding music)",
            f"  distinct channels   {self.distinct_channels:>9,}  (excluding music)",
            "  messiness (of kept rows)",
            f"    removed videos    {self.removed_title_rows:>9,}  {pct(self.removed_title_rows)}",
            f"    no channel info   {self.missing_channel_rows:>9,}  {pct(self.missing_channel_rows)}",
            f"    shared timestamp  {self.same_timestamp_rows:>9,}  {pct(self.same_timestamp_rows)}",
            "SEARCH HISTORY",
            f"  rows in / kept      {self.search_rows_in:,} / {self.search_rows_kept:,}",
        ]
        for reason, n in self.search_dropped.most_common():
            lines.append(f"    dropped: {reason:<14}{n:>9,}")
        subs = "not in export" if self.subscriptions is None else f"{self.subscriptions:,}"
        lines += ["SUBSCRIPTIONS", f"  channels            {subs:>9}"]
        return "\n".join(lines)


@dataclass
class Parsed:
    watch: pl.DataFrame
    search: pl.DataFrame
    subscriptions: pl.DataFrame
    quality: QualityReport


def _load_json_array(raw: bytes) -> list[dict]:
    try:
        data = json.loads(raw.decode("utf-8-sig"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise NoYouTubeData(f"watch history is not valid JSON: {exc}") from exc
    if not isinstance(data, list):
        raise NoYouTubeData("watch history is not a JSON list")
    return [r for r in data if isinstance(r, dict)]


def _parse_watch(rows: list[dict], q: QualityReport) -> pl.DataFrame:
    q.watch_rows_in = len(rows)
    out: list[dict] = []
    seen: set[tuple[str, datetime]] = set()

    for idx, r in enumerate(rows):
        title_raw = r.get("title")
        if not isinstance(title_raw, str):
            q.dropped["no_title"] += 1
            continue
        if title_raw.startswith("Viewed "):                    # community posts, not videos
            q.dropped["post"] += 1
            continue
        if not title_raw.startswith("Watched"):
            # e.g. "Used Shorts": a feature-use marker with no video behind it
            q.dropped["feature_use" if title_raw.startswith("Used ") else "not_a_watch"] += 1
            continue
        if any("Google Ads" in str(d.get("name", "")) for d in (r.get("details") or []) if isinstance(d, dict)):
            q.dropped["ad"] += 1
            continue

        vid, is_short = video_id_from_url(r.get("titleUrl"))
        if vid is None:
            q.dropped["no_video_id"] += 1
            continue
        ts = parse_time(r.get("time"))
        if ts is None:
            q.dropped["bad_time"] += 1
            continue
        if (vid, ts) in seen:
            q.dropped["duplicate"] += 1
            continue
        seen.add((vid, ts))

        title = clean_text(title_raw[len("Watched"):])
        removed = title is None or title.startswith(("http://", "https://"))
        if removed:
            title = None
            q.removed_title_rows += 1

        sub = next((s for s in (r.get("subtitles") or []) if isinstance(s, dict)), None)
        name = clean_text(sub.get("name")) if sub else None
        cid = channel_id_from_url(sub.get("url")) if sub else None
        if name is None and cid is None:
            q.missing_channel_rows += 1
            key = None
        else:
            if cid is None:
                q.channels_without_id += 1
            key = cid or f"name:{name}"

        music = r.get("header") == "YouTube Music" or "music.youtube.com" in str(r.get("titleUrl"))
        if music:
            q.music_rows += 1

        out.append({
            "src_idx": idx, "video_id": vid, "title": title, "title_removed": removed,
            "channel_id": cid, "channel_name": name, "channel_key": key,
            "url_is_short": is_short, "is_music": music, "ts_utc": ts,
        })

    if not out:
        raise NoYouTubeData("no usable watch events")

    df = pl.DataFrame(out, schema=WATCH_SCHEMA)
    # Chronological. The file is newest-first, so for identical timestamps the row that
    # appears LATER in the file happened earlier.
    df = df.sort(["ts_utc", "src_idx"], descending=[False, True])
    df = df.with_columns(pl.col("ts_utc").dt.convert_time_zone(TZ).alias("ts_ist"))

    q.watch_rows_kept = df.height
    q.same_timestamp_rows = int(df.filter(pl.col("ts_utc").is_duplicated()).height)
    video_rows = df.filter(~pl.col("is_music"))          # distinct counts describe YouTube, not Music
    q.distinct_videos = video_rows["video_id"].n_unique()
    q.distinct_channels = video_rows["channel_key"].drop_nulls().n_unique()
    first, last = df["ts_ist"].min(), df["ts_ist"].max()
    q.first_ist, q.last_ist = first.isoformat(timespec="minutes"), last.isoformat(timespec="minutes")
    days = df["ts_ist"].dt.date()
    q.active_days = days.n_unique()
    q.span_days = (days.max() - days.min()).days + 1
    return df


def _parse_search(raw: bytes | None, q: QualityReport) -> pl.DataFrame:
    if raw is None:
        return pl.DataFrame(schema=SEARCH_SCHEMA)
    try:
        rows = _load_json_array(raw)
    except NoYouTubeData:
        return pl.DataFrame(schema=SEARCH_SCHEMA)      # search is optional; never fail the run
    q.search_rows_in = len(rows)
    out = []
    for idx, r in enumerate(rows):
        t = r.get("title")
        if not isinstance(t, str) or not t.startswith("Searched for"):
            q.search_dropped["not_a_search"] += 1
            continue
        query = clean_text(t[len("Searched for"):])
        ts = parse_time(r.get("time"))
        if query is None:
            q.search_dropped["empty"] += 1
        elif ts is None:
            q.search_dropped["bad_time"] += 1
        else:
            out.append({"src_idx": idx, "query": query, "ts_utc": ts})
    q.search_rows_kept = len(out)
    df = pl.DataFrame(out, schema=SEARCH_SCHEMA).sort(["ts_utc", "src_idx"], descending=[False, True])
    return df.with_columns(pl.col("ts_utc").dt.convert_time_zone(TZ).alias("ts_ist"))


def _parse_subs(raw: bytes | None, q: QualityReport) -> pl.DataFrame:
    if raw is None:
        return pl.DataFrame(schema=SUBS_SCHEMA)
    text = raw.decode("utf-8-sig", errors="replace")
    out = []
    for row in csv.DictReader(io.StringIO(text)):
        norm = {(k or "").strip().lower(): v for k, v in row.items()}
        cid = (norm.get("channel id") or "").strip() or None
        name = clean_text(norm.get("channel title"))
        if cid or name:
            out.append({"channel_id": cid, "channel_url": (norm.get("channel url") or "").strip() or None,
                        "channel_name": name})
    q.subscriptions = len(out)
    return pl.DataFrame(out, schema=SUBS_SCHEMA)


def parse_takeout(files: TakeoutFiles) -> Parsed:
    q = QualityReport(members_in_zip=files.member_count)
    watch = _parse_watch(_load_json_array(files.watch), q)
    search = _parse_search(files.search, q)
    subs = _parse_subs(files.subscriptions, q)
    return Parsed(watch=watch, search=search, subscriptions=subs, quality=q)
