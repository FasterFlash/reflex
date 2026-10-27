"""`reflex inspect takeout.zip` prints what is in a file and what was cleaned."""
from __future__ import annotations

import argparse
import json
import sys
import time

from .errors import ReflexError
from pathlib import Path

import polars as pl

from .config import get_setting, youtube_keys
from .enrich import (EnrichError, MetaCache, enrich, fetch_categories, rotating_fetcher,
                     meta_frame, plan)
from .ingest import open_takeout
from .parse import parse_takeout


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="reflex")
    sub = ap.add_subparsers(dest="cmd", required=True)
    ins = sub.add_parser("inspect", help="parse a Takeout ZIP and print the data-quality report")
    ins.add_argument("zip")
    ins.add_argument("--json", action="store_true", help="machine-readable output")
    enr = sub.add_parser("enrich", help="look up video length and category (cached), then summarise")
    enr.add_argument("zip")
    enr.add_argument("--cache", default="data/cache.sqlite")
    enr.add_argument("--dry-run", action="store_true", help="show what would be fetched; needs no API key")
    ana = sub.add_parser("analyze", help="Short/Video split and watch time (needs a cache from `enrich`)")
    ana.add_argument("zip")
    ana.add_argument("--cache", default="data/cache.sqlite")
    rep = sub.add_parser("report", help="build the full report (needs a cache from `enrich`)")
    rep.add_argument("zip")
    rep.add_argument("--cache", default="data/cache.sqlite")
    rep.add_argument("--out", help="write the report JSON here")
    srv = sub.add_parser("serve", help="run the site: UI pages plus the upload API")
    srv.add_argument("--cache", default="data/cache.sqlite")
    srv.add_argument("--web", default="..", help="folder holding index.html, upload.html, ... (default: the folder above backend)")
    srv.add_argument("--host", default="127.0.0.1")
    srv.add_argument("--port", type=int, default=8000)
    fbp = sub.add_parser("feedback", help="print the latest feedback messages")
    fbp.add_argument("--cache", default="data/cache.sqlite")
    fbp.add_argument("-n", type=int, default=20)
    sub.add_parser("check-ai", help="try every Gemini key and model with fake data (prints no keys)")
    args = ap.parse_args(argv)

    if args.cmd == "feedback":
        from pathlib import Path
        from .feedback import FeedbackStore
        rows = FeedbackStore(str(Path(args.cache).resolve().with_name("feedback.sqlite"))).recent(args.n)
        if not rows:
            print("No feedback yet.")
        for r in rows:
            when = time.strftime("%Y-%m-%d %H:%M", time.localtime(r["ts"]))
            stars = f"{r['rating']}/5" if r.get("rating") else ""
            print(f"[{when}] " + " ".join(x for x in (stars, r["kind"]) if x) + (f"  (reply: {r['contact']})" if r["contact"] else ""))
            print(("  " + r["message"].replace("\n", "\n  ") if r["message"] else "  (no message)") + "\n")
        return 0
    if args.cmd == "check-ai":
        return _check_ai()
    if args.cmd == "serve":
        return _serve(args)
    if args.cmd == "enrich":
        return _enrich(args)
    if args.cmd == "analyze":
        return _analyze(args)
    if args.cmd == "report":
        return _report(args)

    t0 = time.perf_counter()
    try:
        parsed = parse_takeout(open_takeout(args.zip))
    except ReflexError as exc:
        print(f"[{exc.code}] {exc}", file=sys.stderr)
        return 2
    elapsed = time.perf_counter() - t0

    if args.json:
        print(json.dumps(parsed.quality.to_dict(), indent=2))
    else:
        print(parsed.quality.render())
        print(f"\nparsed in {elapsed:.2f}s")
    return 0


def _check_ai() -> int:
    """One real call per (key, model) on a made-up fact sheet. Prints the key's position and the model name, never the key."""
    from . import taglines as tl

    keys, models = tl.load_keys(), tl.load_models()
    if not keys:
        print("No Gemini keys found. Put GEMINI_API_KEY_1 (and _2) in backend\\.env.", file=sys.stderr)
        return 2
    facts = tl.clean_facts({
        "hours": 154.2, "days": 91, "videos_time_pct": 81, "shorts_time_pct": 19, "shorts_plays": 6964,
        "longest_sitting_min": 198, "longest_shorts_scroll": 98, "peak_time_of_day": "evening",
        "category_mix": [{"category": "Entertainment", "pct": 32}, {"category": "Comedy", "pct": 15}, {"category": "Music", "pct": 13}],
        "top_channels": [{"name": "Channel A", "hours": 15.0}, {"name": "Channel B", "hours": 7.2}, {"name": "Channel C", "hours": 6.7}]})
    body = tl.build_request(facts, [], None)
    print(f"{len(keys)} key(s), models: {', '.join(models)}\n")
    bad = 0
    for ki, key in enumerate(keys, 1):
        for model in models:
            t0 = time.perf_counter()
            try:
                raw = tl.gemini_call(key, model, body)
            except tl.KeyRejected as e:
                print(f"key {ki}  {model:<28} REJECTED ({e}) - wrong key, key not enabled, or unknown model name"); bad += 1; continue
            except Exception as e:
                print(f"key {ki}  {model:<28} FAILED ({e}) - 404 = wrong model name, 429 = out of quota, 503 = Google busy"); bad += 1; continue
            out = tl.parse_and_validate(raw, facts, [])
            dt = time.perf_counter() - t0
            if out:
                print(f"key {ki}  {model:<28} OK  {dt:.1f}s  {len(out)} usable taglines")
                for t in out:
                    print(f"          [{t['tone']}] {t['text']}")
            else:
                print(f"key {ki}  {model:<28} ANSWERED but the output failed validation ({dt:.1f}s)"); bad += 1
    return 1 if bad else 0


def _serve(args) -> int:
    import uvicorn

    from .server import create_app

    web = Path(args.web).resolve()
    if not (web / "index.html").is_file():
        print(f"No index.html in {web}. Point --web at the folder that holds the pages.", file=sys.stderr)
        return 2
    if not youtube_keys():
        print("note: no YOUTUBE_API_KEY found. Uploads work only for videos already in the cache.", file=sys.stderr)
    app = create_app(cache_path=args.cache, web_dir=web)
    print(f"Reflex at http://{args.host}:{args.port}   (Ctrl+C to stop)")
    uvicorn.run(app, host=args.host, port=args.port, log_level="warning")
    return 0


def _length_buckets(df: pl.DataFrame) -> list[tuple[str, int]]:
    d = pl.col("duration_s")
    rows = [
        ("60 s or less", df.filter(d <= 60).height),
        ("1 to 3 min", df.filter((d > 60) & (d <= 180)).height),
        ("over 3 min", df.filter(d > 180).height),
        ("live / premiere", df.filter(d.is_null() & (pl.col("meta_status") == "ok")).height),
        ("removed or private", df.filter(pl.col("meta_status") == "unavailable").height),
        ("not looked up", df.filter(pl.col("meta_status").is_null()).height),
    ]
    return rows


def _analyze(args) -> int:
    from .watchtime import build_events, summarize

    try:
        parsed = parse_takeout(open_takeout(args.zip))
    except ReflexError as exc:
        print(f"[{exc.code}] {exc}", file=sys.stderr)
        return 2
    cache = MetaCache(args.cache)
    ids = parsed.watch.filter(~pl.col("is_music"))["video_id"].unique().to_list()
    if not cache.get_many(ids[:50]):
        print("The cache is empty for this file. Run `reflex enrich` first.", file=sys.stderr)
        return 3
    a = build_events(parsed.watch, meta_frame(cache, ids))
    s = summarize(a)
    t, per = s["total_hours"], s["hours_per_day"]
    print(f"{s['events']:,} events over {s['days']} days, {s['sessions']} sessions\n")
    print(f"WATCH TIME   floor {t['floor']:.0f} h   |   estimate {t['estimate']:.0f} h   |   ceiling {t['ceiling']:.0f} h")
    print(f"PER DAY      floor {per['floor']:.1f} h   |   estimate {per['estimate']:.1f} h   |   ceiling {per['ceiling']:.1f} h")
    iso = s["isolated"]
    print(f"{iso['events']:,} events were the last before 30+ min of silence: {iso['share_of_ceiling']:.0%} of the ceiling rests on them\n")
    print(f"{'':<10}{'events':>8}{'share':>8}{'est. h':>9}{'share':>8}")
    for k, v in s["by_kind"].items():
        print(f"{k:<10}{v['events']:>8,}{v['events'] / s['events']:>8.1%}{v['estimate']:>9.1f}{(v['estimate'] / t['estimate'] if t['estimate'] else 0):>8.1%}")
    print("\ncompletion rate used for isolated events, by length:")
    for b, r in sorted(s["completion_rates"].items()):
        print(f"  {b:<10}{r:.0%}")
    return 0


def _report(args) -> int:
    from .report import build_report

    try:
        parsed = parse_takeout(open_takeout(args.zip))
    except ReflexError as exc:
        print(f"[{exc.code}] {exc}", file=sys.stderr)
        return 2
    cache = MetaCache(args.cache)
    ids = parsed.watch.filter(~pl.col("is_music"))["video_id"].unique().to_list()
    if not cache.get_many(ids[:50]):
        print("The cache is empty for this file. Run `reflex enrich` first.", file=sys.stderr)
        return 3
    t0 = time.perf_counter()
    r = build_report(parsed, meta_frame(cache, ids), cache.categories())
    took = time.perf_counter() - t0
    if args.out:
        Path(args.out).parent.mkdir(parents=True, exist_ok=True)
        Path(args.out).write_text(json.dumps(r, indent=2, ensure_ascii=False), encoding="utf-8")
        print(f"wrote {args.out}")
    print(_render_report(r))
    print(f"\nbuilt in {took:.2f}s")
    return 0


def _render_report(r: dict) -> str:
    L = []
    t, k = r["totals"], r["kinds"]
    L += [f"{t['events']:,} watches, {r['range']['active_days']} of {r['range']['span_days']} days, {t['sessions']} sessions",
          f"HOURS  {t['hours']['estimate']:.0f}  (range {t['hours']['floor']:.0f}-{t['hours']['ceiling']:.0f}), {t['hours_per_day']['estimate']:.1f} h/day",
          f"SHORTS {k['short']['events_pct']:.0f}% of watches, {k['short']['time_pct']:.0f}% of time   |   VIDEOS {k['video']['events_pct']:.0f}% of watches, {k['video']['time_pct']:.0f}% of time"]
    ego = r["alter_ego"]
    if ego:
        L += ["", f"ALTER EGO  {ego['name']}", *[f"  {x}" for x in ego["evidence"]]]
    L += ["", "TOP CHANNELS (by time)"]
    L += [f"  {c['name'][:34]:<34} {c['hours']:>6.1f} h  {c['events']:>5} watches  {'sub' if c['subscribed'] else ''}" for c in r["channels"]["top_by_time"]]
    L += ["TOP CHANNELS (by watches)"]
    L += [f"  {c['name'][:34]:<34} {c['events']:>6} watches  {c['hours']:>5.1f} h" for c in r["channels"]["top_by_events"]]
    L += ["", "TOP CATEGORIES (by time)"]
    L += [f"  {c['category']:<26}{c['hours']:>7.1f} h  {c['share_of_time_pct']:>5.1f}%" for c in r["categories"]["top_by_time"]]
    s = r["searches"]
    L += ["", f"TOP SEARCHES  ({s['total']} searches, {s['distinct']} distinct)"]
    L += [f"  {x['query'][:40]:<40} {x['times']}x on {x['on_days']} days" for x in s["top"]]
    sb = r["subscriptions"]
    if sb:
        L += ["", f"SUBSCRIPTIONS  {sb['subscribed']:,}; watched {sb['watched_at_least_once']:,}; never watched {sb['never_watched']:,} ({sb['never_watched_pct']:.0f}%)",
              f"  subscribed channels are {sb['share_of_events_pct']:.0f}% of watches and {sb['share_of_time_pct']:.0f}% of time"]
    c = r["calendar"]
    L += ["", f"CALENDAR  longest streak {c['longest_streak']['days']} days, busiest day {c['busiest_day']['date']} ({c['busiest_day']['hours']} h), typical {c['typical_day_hours']} h",
          f"  weekday avg {c['weekday_avg_hours']} h, weekend avg {c['weekend_avg_hours']} h"]
    h = r["rhythm"]
    L += [f"RHYTHM    peak hour {h['peak_hour']:02d}:00, peak day {h['peak_weekday']}; time by slot {h['share_of_time_pct']}"]
    ss = r["sessions"]
    L += [f"SESSIONS  median {ss['median_minutes']} min / {ss['median_events']:.0f} watches; longest {ss['longest']['minutes']} min ({ss['longest']['events']} watches)",
          f"  longest Shorts scroll: {ss['longest_shorts_scroll']['events']} Shorts in {ss['longest_shorts_scroll']['minutes']} min"]
    rw = r["rewatches"]
    L += ["", f"REWATCHES  {rw['videos_watched_more_than_once']} videos watched more than once ({rw['extra_watches']} extra watches)"]
    L += [f"  {x['watches']}x  {x['title'][:60]}" for x in rw["top"]]
    f = r["facts"]
    if f["latest_night"]:
        L += [f"LATEST NIGHT  {f['latest_night']['clock']} on a {f['latest_night']['weekday']} ({f['latest_night']['date']})"]
    L += ["", "DISCLOSURES", *[f"  - {d}" for d in r["disclosures"]]]
    return "\n".join(L)


def _enrich(args) -> int:
    try:
        parsed = parse_takeout(open_takeout(args.zip))
    except ReflexError as exc:
        print(f"[{exc.code}] {exc}", file=sys.stderr)
        return 2

    watch = parsed.watch.filter(~pl.col("is_music"))
    ids = watch["video_id"].unique().to_list()
    cache = MetaCache(args.cache)
    todo, hits = plan(ids, cache)
    calls = -(-len(todo) // 50)
    print(f"unique videos       {len(ids):>7,}")
    print(f"already cached      {hits:>7,}")
    print(f"to look up          {len(todo):>7,}  (~{calls} API calls, ~{calls + (0 if cache.categories() else 1)} quota units)")

    if args.dry_run:
        return 0

    keys = youtube_keys()
    if not keys:
        print("No YOUTUBE_API_KEY found. Put it in backend\\.env or set the environment variable.", file=sys.stderr)
        return 3

    fetch = rotating_fetcher(keys)
    t0 = time.perf_counter()
    try:
        fetch_categories(cache, fetch)
        stats = enrich(ids, cache, fetch,
                       on_batch=lambda s: print(f"\r  looked up {s.fetched_ok + s.unavailable:,}/{len(todo):,}", end="", flush=True))
    except EnrichError as exc:
        print(f"\n[api] {exc}", file=sys.stderr)
        return 4
    print()
    print(f"calls made          {stats.calls:>7,}   in {time.perf_counter() - t0:.1f}s")
    print(f"found               {stats.fetched_ok:>7,}")
    print(f"removed/private     {stats.unavailable:>7,}")
    if stats.stopped == "quota":
        print(f"STOPPED: daily quota used up. {stats.remaining:,} left; run again tomorrow, progress is saved.")

    joined = watch.join(meta_frame(cache, ids), on="video_id", how="left")
    total = joined.height
    print("\nWATCH EVENTS BY VIDEO LENGTH")
    for label, n in _length_buckets(joined):
        print(f"  {label:<20}{n:>8,}  {n / total:6.1%}")
    cats = cache.categories()
    top = (joined.filter(pl.col("category_id").is_not_null()).group_by("category_id").len()
           .sort("len", descending=True).head(6))
    print("\nTOP CATEGORIES (by events)")
    for cid, n in top.iter_rows():
        print(f"  {cats.get(cid, f'#{cid}'):<24}{n:>6,}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
