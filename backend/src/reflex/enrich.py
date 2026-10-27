"""Look up video length and category from the YouTube Data API, with a persistent cache.

Why this exists: Takeout has no video length, and length is what separates Shorts from
videos and makes "watch time" computable at all.

What is cached: public facts about a video (length, category, channel). Never who
watched it, when, or anything from a user's history.

Cost: videos.list is 1 quota unit per call of up to 50 ids, so ~170 calls for a
history with ~8,500 unique videos. Calls are resumable: every batch is written to the
cache before the next is requested.
"""
from __future__ import annotations

import json
import re
import sqlite3
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable, Iterable

import polars as pl

API = "https://www.googleapis.com/youtube/v3"
BATCH = 50
UNAVAILABLE_RETRY_DAYS = 30

Fetcher = Callable[[str, dict], dict]


class EnrichError(Exception):
    pass


class BadKey(EnrichError):
    pass


class QuotaExceeded(EnrichError):
    pass


@dataclass
class VideoMeta:
    video_id: str
    status: str                      # "ok" | "unavailable"
    duration_s: int | None = None    # None for live/upcoming/unavailable
    category_id: int | None = None
    channel_id: str | None = None
    live: str | None = None          # "none" | "live" | "upcoming"
    shorts_hint: bool = False        # "#shorts" in title, description or tags
    fetched_at: str = ""


_DURATION = re.compile(r"^P(?:(\d+)D)?(?:T(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?)?$")


def parse_duration(value: object) -> int | None:
    """ISO 8601 'PT1H2M3S' -> seconds. 'P0D' (live/premiere placeholder) and junk -> None."""
    if not isinstance(value, str):
        return None
    m = _DURATION.match(value)
    if not m:
        return None
    d, h, mi, s = (int(x) if x else 0 for x in m.groups())
    total = d * 86400 + h * 3600 + mi * 60 + s
    return total or None


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def meta_from_item(item: dict) -> VideoMeta:
    sn = item.get("snippet") or {}
    cd = item.get("contentDetails") or {}
    text = (str(sn.get("title", "")) + " " + str(sn.get("description", ""))).lower()
    tags = {str(t).lower().lstrip("#") for t in (sn.get("tags") or [])}
    cat = sn.get("categoryId")
    return VideoMeta(
        video_id=item["id"], status="ok",
        duration_s=parse_duration(cd.get("duration")),
        category_id=int(cat) if str(cat).isdigit() else None,
        channel_id=sn.get("channelId"),
        live=sn.get("liveBroadcastContent"),
        shorts_hint="#short" in text or bool(tags & {"short", "shorts", "youtubeshorts", "ytshorts"}),
        fetched_at=_now(),
    )


class MetaCache:
    def __init__(self, path: str | Path = ":memory:"):
        if str(path) != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(str(path))
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS video_meta(
                video_id TEXT PRIMARY KEY, status TEXT NOT NULL, duration_s INTEGER,
                category_id INTEGER, channel_id TEXT, live TEXT, shorts_hint INTEGER, fetched_at TEXT);
            CREATE TABLE IF NOT EXISTS categories(id INTEGER PRIMARY KEY, title TEXT NOT NULL);
        """)

    def get_many(self, ids: Iterable[str]) -> dict[str, VideoMeta]:
        ids = list(ids)
        out: dict[str, VideoMeta] = {}
        for i in range(0, len(ids), 500):
            chunk = ids[i:i + 500]
            q = ",".join("?" * len(chunk))
            for r in self.db.execute(f"SELECT video_id,status,duration_s,category_id,channel_id,live,shorts_hint,fetched_at "
                                     f"FROM video_meta WHERE video_id IN ({q})", chunk):
                out[r[0]] = VideoMeta(r[0], r[1], r[2], r[3], r[4], r[5], bool(r[6]), r[7])
        return out

    def put_many(self, metas: Iterable[VideoMeta]) -> None:
        with self.db:
            self.db.executemany(
                "INSERT OR REPLACE INTO video_meta VALUES(?,?,?,?,?,?,?,?)",
                [(m.video_id, m.status, m.duration_s, m.category_id, m.channel_id, m.live, int(m.shorts_hint), m.fetched_at)
                 for m in metas])

    def categories(self) -> dict[int, str]:
        return dict(self.db.execute("SELECT id,title FROM categories"))

    def put_categories(self, cats: dict[int, str]) -> None:
        with self.db:
            self.db.executemany("INSERT OR REPLACE INTO categories VALUES(?,?)", list(cats.items()))

    def count(self) -> int:
        return self.db.execute("SELECT COUNT(*) FROM video_meta").fetchone()[0]


def http_fetcher(api_key: str, *, retries: int = 3, timeout: float = 20.0, sleep=time.sleep) -> Fetcher:
    """Real HTTP. The key goes in a header, and no error message ever contains it or the URL."""

    def fetch(endpoint: str, params: dict) -> dict:
        qs = "&".join(f"{k}={urllib.parse.quote(str(v), safe=',')}" for k, v in params.items())
        req = urllib.request.Request(f"{API}/{endpoint}?{qs}", headers={"X-Goog-Api-Key": api_key})
        for attempt in range(retries + 1):
            try:
                with urllib.request.urlopen(req, timeout=timeout) as resp:
                    return json.load(resp)
            except urllib.error.HTTPError as e:
                try:
                    reason = json.load(e)["error"]["errors"][0]["reason"]
                except Exception:
                    reason = ""
                if e.code == 403 and reason in ("quotaExceeded", "dailyLimitExceeded", "rateLimitExceeded"):
                    raise QuotaExceeded("daily API quota used up") from None
                if e.code in (400, 403) and reason in ("keyInvalid", "API_KEY_INVALID", "accessNotConfigured", "ipRefererBlocked"):
                    raise BadKey(f"the API key was rejected ({reason})") from None
                if e.code in (429, 500, 502, 503, 504) and attempt < retries:
                    sleep(2 ** attempt)
                    continue
                raise EnrichError(f"YouTube API returned HTTP {e.code}") from None
            except (urllib.error.URLError, TimeoutError, OSError):
                if attempt < retries:
                    sleep(2 ** attempt)
                    continue
                raise EnrichError("could not reach the YouTube API") from None
        raise EnrichError("unreachable")

    return fetch


def rotating_fetcher(keys: list[str], *, cooldown_s: float = 3600.0, clock=time.monotonic, **kw) -> Fetcher:
    """Try keys in order; when one hits its quota (or is rejected) move on to the next.

    A spent key is skipped for `cooldown_s`, then given another chance (quota resets daily).
    Safe to call from several worker threads. Only when every key is spent does it raise.
    """
    import threading

    fetchers = [http_fetcher(k, **kw) for k in keys]
    dead_until = [0.0] * len(fetchers)
    lock = threading.Lock()

    def fetch(endpoint: str, params: dict) -> dict:
        last: Exception | None = None
        for i, f in enumerate(fetchers):
            with lock:
                if dead_until[i] > clock():
                    continue
            try:
                return f(endpoint, params)
            except (QuotaExceeded, BadKey) as exc:
                last = exc
                with lock:
                    dead_until[i] = clock() + (cooldown_s if isinstance(exc, QuotaExceeded) else 1e12)
        if last is None:
            raise QuotaExceeded("daily API quota used up")
        raise last

    return fetch


@dataclass
class EnrichStats:
    requested: int = 0
    cache_hits: int = 0
    fetched_ok: int = 0
    unavailable: int = 0
    calls: int = 0
    stopped: str | None = None       # "quota" if the run ended early
    notes: list[str] = field(default_factory=list)

    @property
    def remaining(self) -> int:
        return self.requested - self.cache_hits - self.fetched_ok - self.unavailable


def _stale(m: VideoMeta) -> bool:
    if m.status != "unavailable":
        return False
    try:
        age = datetime.now(timezone.utc) - datetime.fromisoformat(m.fetched_at)
    except ValueError:
        return True
    return age > timedelta(days=UNAVAILABLE_RETRY_DAYS)


def plan(video_ids: Iterable[str], cache: MetaCache) -> tuple[list[str], int]:
    """(ids that need a lookup, how many are already cached)."""
    ids = list(dict.fromkeys(video_ids))
    have = cache.get_many(ids)
    todo = [i for i in ids if i not in have or _stale(have[i])]
    return todo, len(ids) - len(todo)


def enrich(video_ids: Iterable[str], cache: MetaCache, fetch: Fetcher, *,
           on_batch: Callable[[EnrichStats], None] | None = None, workers: int = 1) -> EnrichStats:
    """Look up every id the cache doesn't have.

    workers > 1 only parallelises the HTTP calls. Results are applied one batch at a time,
    in order, on the calling thread, so the SQLite cache is never touched from two threads.
    """
    ids = list(dict.fromkeys(video_ids))
    todo, hits = plan(ids, cache)
    stats = EnrichStats(requested=len(ids), cache_hits=hits)
    batches = [todo[i:i + BATCH] for i in range(0, len(todo), BATCH)]

    def call(batch: list[str]) -> dict:
        return fetch("videos", {
            "part": "contentDetails,snippet", "id": ",".join(batch), "maxResults": BATCH,
            "fields": "items(id,contentDetails/duration,snippet(title,description,tags,categoryId,channelId,liveBroadcastContent))",
        })

    def apply(batch: list[str], data: dict) -> None:
        stats.calls += 1
        asked = set(batch)
        found = {it["id"]: meta_from_item(it) for it in data.get("items", []) if it.get("id") in asked}
        gone = [VideoMeta(v, "unavailable", fetched_at=_now()) for v in batch if v not in found]
        cache.put_many([*found.values(), *gone])
        stats.fetched_ok += len(found)
        stats.unavailable += len(gone)
        if on_batch:
            on_batch(stats)

    if workers <= 1:
        for batch in batches:
            try:
                data = call(batch)
            except QuotaExceeded:
                stats.stopped = "quota"
                break
            apply(batch, data)
        return stats

    with ThreadPoolExecutor(max_workers=workers) as pool:
        queue = iter(batches)
        pending: deque = deque()

        def submit() -> None:
            b = next(queue, None)
            if b is not None:
                pending.append((b, pool.submit(call, b)))

        for _ in range(workers):
            submit()
        while pending:
            batch, fut = pending.popleft()
            try:
                data = fut.result()
            except QuotaExceeded:
                stats.stopped = "quota"
                break
            except BaseException:
                for _, f in pending:
                    f.cancel()
                raise
            apply(batch, data)
            submit()
        for _, f in pending:
            f.cancel()
    return stats


def fetch_categories(cache: MetaCache, fetch: Fetcher, region: str = "IN") -> dict[int, str]:
    have = cache.categories()
    if have:
        return have
    data = fetch("videoCategories", {"part": "snippet", "regionCode": region, "fields": "items(id,snippet/title)"})
    cats = {int(i["id"]): i["snippet"]["title"] for i in data.get("items", []) if str(i.get("id", "")).isdigit()}
    cache.put_categories(cats)
    return cats


def meta_frame(cache: MetaCache, video_ids: Iterable[str]) -> pl.DataFrame:
    got = cache.get_many(list(dict.fromkeys(video_ids)))
    return pl.DataFrame(
        [{"video_id": m.video_id, "meta_status": m.status, "duration_s": m.duration_s,
          "category_id": m.category_id, "api_channel_id": m.channel_id, "live": m.live,
          "shorts_hint": m.shorts_hint} for m in got.values()],
        schema={"video_id": pl.String, "meta_status": pl.String, "duration_s": pl.Int64,
                "category_id": pl.Int64, "api_channel_id": pl.String, "live": pl.String,
                "shorts_hint": pl.Boolean})
