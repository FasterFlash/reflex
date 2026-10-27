"""ZIP -> report, in one call. The server and any script share this; it does no HTTP serving itself."""
from __future__ import annotations

from typing import Callable

import polars as pl

from .enrich import EnrichError, MetaCache, enrich, fetch_categories, meta_frame, plan
from .errors import LookupFailed
from .ingest import TakeoutFiles
from .parse import parse_takeout
from .report import build_report

Progress = Callable[[str, int, int], None]      # (stage, done, total)
STAGES = ("reading", "lookup", "measuring")


def analyse(files: TakeoutFiles, *, cache_path: str, make_fetcher: Callable[[], object | None],
            progress: Progress | None = None, workers: int = 8) -> dict:
    say = progress or (lambda *_: None)

    say("reading", 0, 0)
    parsed = parse_takeout(files)
    ids = parsed.watch.filter(~pl.col("is_music"))["video_id"].unique().to_list()

    cache = MetaCache(cache_path)
    todo, _ = plan(ids, cache)
    stats = None
    if todo or not cache.categories():
        fetch = make_fetcher()
        if fetch is None:
            raise LookupFailed("no API key configured")
        say("lookup", 0, len(todo))
        try:
            fetch_categories(cache, fetch)
            stats = enrich(ids, cache, fetch, workers=workers,
                           on_batch=lambda s: say("lookup", s.fetched_ok + s.unavailable, len(todo)))
        except EnrichError as exc:
            raise LookupFailed(str(exc)) from None      # EnrichError text never contains the key

    say("measuring", 0, 0)
    report = build_report(parsed, meta_frame(cache, ids), cache.categories())
    slices = [report] + [m["report"] for m in report.get("months", [])]
    if stats is not None and stats.stopped:
        left = stats.remaining
        for rep in slices:
            rep["lookup"] = {"complete": False, "missing_videos": left}
            rep["disclosures"].append(
                f"The video lookup stopped early (daily quota), so {left:,} videos have no length. "
                "Their time is missing from every number here.")
    else:
        for rep in slices:
            rep["lookup"] = {"complete": True, "missing_videos": 0}
    return report
