"""Turn analysed events into the report: one JSON-serialisable dict.

Everything here is counting and ranking. No AI, no network. Times are IST, and a
watch is attributed to the day, hour and weekday it STARTED. All hours are the
watch-time estimate; floor and ceiling travel alongside where it matters.
"""
from __future__ import annotations

import re
import unicodedata
from datetime import date, timedelta
from statistics import median

import polars as pl

from .parse import Parsed
from .watchtime import Analysis, build_events, summarize

SCHEMA_VERSION = 2
WEEKDAYS = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]
REWATCH_GAP_S = 10 * 60          # events of one video closer than this are the same watch

# YouTube category id -> noun for the alter ego
EGO_NOUN = {
    1: "Cinephile", 2: "Gearhead", 10: "Melomaniac", 15: "Animal Whisperer", 17: "Armchair Athlete",
    19: "Daydreaming Traveler", 20: "Gamer", 22: "Lurker", 23: "Jester", 24: "Spectator",
    25: "Pundit", 26: "Tinkerer", 27: "Scholar", 28: "Gadget Scholar", 29: "Activist",
}
SLOTS = {"night": range(0, 6), "morning": range(6, 12), "afternoon": range(12, 18), "evening": range(18, 24)}
SLOT_ADJ = {"night": "Night", "morning": "Morning", "afternoon": "Afternoon", "evening": "Evening"}
NIGHT_THRESHOLD = 0.25


def _h(seconds: float | None) -> float:
    return round((seconds or 0.0) / 3600, 2)


def _pct(part: float, whole: float) -> float:
    return round(100 * part / whole, 1) if whole else 0.0


def _iso(d) -> str | None:
    return d.isoformat() if d is not None else None


def _clock(ts) -> str:
    return ts.strftime("%H:%M")


EST = pl.col("estimate_s").fill_null(0.0)
FLO = pl.col("floor_s").fill_null(0.0)
CEI = pl.col("ceiling_s").fill_null(0.0)


# --- channels ---------------------------------------------------------------------------

def _channels(e: pl.DataFrame, subscribed_ids: set[str], total_est: float) -> dict:
    c = e.filter(pl.col("channel_key").is_not_null())
    g = c.group_by("channel_key").agg(
        pl.col("channel_id").first().alias("channel_id"),
        pl.col("channel_name").drop_nulls().last().alias("name"),
        pl.len().alias("events"),
        pl.col("video_id").n_unique().alias("videos"),
        EST.sum().alias("est"), FLO.sum().alias("floor"), CEI.sum().alias("ceiling"),
    )

    def rows(df):
        return [{"name": r["name"], "events": r["events"], "distinct_videos": r["videos"],
                 "hours": _h(r["est"]), "hours_floor": _h(r["floor"]), "hours_ceiling": _h(r["ceiling"]),
                 "share_of_time_pct": _pct(r["est"], total_est),
                 "subscribed": r["channel_id"] in subscribed_ids}
                for r in df.iter_rows(named=True)]

    return {
        "distinct": g.height,
        "events_without_channel": e.height - c.height,
        "top_by_time": rows(g.sort(["est", "events"], descending=True).head(5)),
        "top_by_events": rows(g.sort(["events", "est"], descending=True).head(5)),
    }


# --- categories -------------------------------------------------------------------------

def _categories(e: pl.DataFrame, names: dict[int, str]) -> dict:
    k = e.filter(pl.col("category_id").is_not_null())
    total_est = float(k["estimate_s"].sum() or 0.0)
    g = k.group_by("category_id").agg(pl.len().alias("events"), EST.sum().alias("est"))

    def rows(df):
        return [{"category": names.get(r["category_id"], f"Category {r['category_id']}"), "category_id": r["category_id"],
                 "events": r["events"], "hours": _h(r["est"]), "share_of_time_pct": _pct(r["est"], total_est)}
                for r in df.iter_rows(named=True)]

    return {
        "events_without_category": e.height - k.height,
        "top_by_time": rows(g.sort(["est", "events"], descending=True).head(5)),
        "top_by_events": rows(g.sort(["events", "est"], descending=True).head(5)),
    }


# --- searches ---------------------------------------------------------------------------

_PUNCT = re.compile(r"[^\w\s]", re.UNICODE)
_WS = re.compile(r"\s+")


def normalize_query(q: str) -> str:
    q = unicodedata.normalize("NFKC", q).casefold()
    return _WS.sub(" ", _PUNCT.sub(" ", q)).strip() or q.strip()


def _searches(s: pl.DataFrame) -> dict:
    if s.height == 0:
        return {"total": 0, "distinct": 0, "repeat_searches": 0, "top": []}
    s = s.with_columns(pl.col("query").map_elements(normalize_query, return_dtype=pl.String).alias("norm"),
                       pl.col("ts_ist").dt.date().alias("day"))
    g = s.group_by("norm").agg(
        pl.len().alias("count"), pl.col("day").n_unique().alias("days"),
        pl.col("query").mode().first().alias("shown"),
        pl.col("ts_ist").min().alias("first"), pl.col("ts_ist").max().alias("last"))
    g = g.sort(["count", "last"], descending=True)
    return {
        "total": s.height, "distinct": g.height, "repeat_searches": s.height - g.height,
        "first_day": _iso(s["ts_ist"].min().date()), "last_day": _iso(s["ts_ist"].max().date()),
        "top": [{"query": r["shown"], "times": r["count"], "on_days": r["days"],
                 "first": _iso(r["first"].date()), "last": _iso(r["last"].date())}
                for r in g.head(5).iter_rows(named=True)],
    }


# --- subscriptions ----------------------------------------------------------------------

def _subscriptions(e: pl.DataFrame, subs: pl.DataFrame, in_export: bool, total_est: float) -> dict | None:
    if not in_export:
        return None
    ids = set(subs["channel_id"].drop_nulls().to_list())
    watched_ids = set(e["channel_id"].drop_nulls().to_list())
    mine = e.filter(pl.col("channel_id").is_in(list(ids))) if ids else e.head(0)
    other = e.filter(pl.col("channel_id").is_not_null() & ~pl.col("channel_id").is_in(list(ids)))

    def top(df, n=3):
        g = (df.group_by("channel_id").agg(pl.col("channel_name").drop_nulls().last().alias("name"),
                                           pl.len().alias("events"), EST.sum().alias("est"))
             .sort(["est", "events"], descending=True).head(n))
        return [{"name": r["name"], "events": r["events"], "hours": _h(r["est"])} for r in g.iter_rows(named=True)]

    est_mine = float(mine["estimate_s"].sum() or 0.0)
    return {
        "subscribed": len(ids),
        "watched_at_least_once": len(ids & watched_ids),
        "never_watched": len(ids - watched_ids),
        "never_watched_pct": _pct(len(ids - watched_ids), len(ids)),
        "share_of_events_pct": _pct(mine.height, e.height),
        "share_of_time_pct": _pct(est_mine, total_est),
        "top_subscribed_by_time": top(mine),
        "top_not_subscribed_by_time": top(other),
    }


# --- calendar ---------------------------------------------------------------------------

def _streaks(days: list[date]) -> tuple[dict, dict]:
    best = {"days": 0, "start": None, "end": None}
    run_start, prev, run = None, None, 0
    for d in days:
        if prev is not None and (d - prev).days == 1:
            run += 1
        else:
            run, run_start = 1, d
        if run > best["days"]:
            best = {"days": run, "start": _iso(run_start), "end": _iso(d)}
        prev = d
    current = {"days": run if days else 0, "start": _iso(run_start) if days else None,
               "end": _iso(days[-1]) if days else None}
    return best, current


def _calendar(e: pl.DataFrame) -> dict:
    g = (e.group_by("date").agg(pl.len().alias("events"), EST.sum().alias("est"),
                                (pl.col("kind") == "short").sum().alias("shorts"),
                                (pl.col("kind") == "video").sum().alias("videos"))
         .sort("date"))
    days = g["date"].to_list()
    first, last = days[0], days[-1]
    span = (last - first).days + 1
    est = dict(zip(days, g["est"].to_list()))
    full = [first + timedelta(days=i) for i in range(span)]
    wk = [est.get(d, 0.0) for d in full if d.weekday() < 5]
    we = [est.get(d, 0.0) for d in full if d.weekday() >= 5]
    best, current = _streaks(days)
    top = g.sort("est", descending=True).row(0, named=True)

    by_month: dict[str, list] = {}                      # key -> [hours, days covered]
    for d in full:
        m = by_month.setdefault(d.strftime("%Y-%m"), [0.0, 0])
        m[0] += est.get(d, 0.0); m[1] += 1
    pool = {k: v for k, v in by_month.items() if v[1] >= 14} or by_month     # a 5-day month shouldn't beat a real one
    bk = max(pool, key=lambda k: pool[k][0] / pool[k][1])
    wd_tot, wd_n = [0.0] * 7, [0] * 7
    for d in full:
        wd_tot[d.weekday()] += est.get(d, 0.0); wd_n[d.weekday()] += 1
    bw = max(range(7), key=lambda i: wd_tot[i] / wd_n[i] if wd_n[i] else 0.0)
    return {
        "busiest_month": {"key": bk, "avg_hours": _h(pool[bk][0] / pool[bk][1]), "hours": _h(pool[bk][0]), "days": pool[bk][1]},
        "busiest_weekday": {"name": WEEKDAYS[bw], "avg_hours": _h(wd_tot[bw] / wd_n[bw]) if wd_n[bw] else 0.0},
        "first_day": _iso(first), "last_day": _iso(last), "span_days": span, "active_days": len(days),
        "missed_days": span - len(days),
        "longest_streak": best, "current_streak": current,
        "busiest_day": {"date": _iso(top["date"]), "hours": _h(top["est"]), "events": top["events"]},
        "typical_day_hours": _h(median(g["est"].to_list())),
        "weekday_avg_hours": _h(sum(wk) / len(wk)) if wk else 0.0,
        "weekend_avg_hours": _h(sum(we) / len(we)) if we else 0.0,
        "days": [{"date": _iso(r["date"]), "hours": _h(r["est"]), "events": r["events"],
                  "shorts": r["shorts"], "videos": r["videos"]} for r in g.iter_rows(named=True)],
    }


# --- rhythm -----------------------------------------------------------------------------

def _rhythm(e: pl.DataFrame, total_est: float) -> dict:
    g = e.group_by(["wd", "hour"]).agg(EST.sum().alias("est"))
    matrix = [[0.0] * 24 for _ in range(7)]
    for r in g.iter_rows(named=True):
        matrix[r["wd"]][r["hour"]] = r["est"]
    hour_tot = [sum(matrix[d][h] for d in range(7)) for h in range(24)]
    wd_tot = [sum(row) for row in matrix]
    slots = {name: _pct(sum(hour_tot[h] for h in hrs), total_est) for name, hrs in SLOTS.items()}
    ev_night = e.filter(pl.col("hour") < 6).height
    return {
        "hours_by_weekday_hour": [[round(x / 3600, 3) for x in row] for row in matrix],
        "peak_hour": max(range(24), key=lambda h: hour_tot[h]),
        "peak_weekday": WEEKDAYS[max(range(7), key=lambda d: wd_tot[d])],
        "share_of_time_pct": slots,
        "night_events_pct": _pct(ev_night, e.height),
    }


# --- sessions ---------------------------------------------------------------------------

def _sessions(e: pl.DataFrame) -> dict:
    g = e.group_by("session", maintain_order=True).agg(
        pl.col("ts_ist").first().alias("start"), pl.col("ts_ist").last().alias("end"),
        pl.len().alias("events"), EST.sum().alias("est"), EST.last().alias("last_est"),
        (pl.col("kind") == "short").sum().alias("shorts"))
    g = g.with_columns(((pl.col("end") - pl.col("start")).dt.total_seconds() + pl.col("last_est")).alias("span_s"))
    longest = g.sort("span_s", descending=True).row(0, named=True)

    # longest unbroken run of Shorts inside one sitting
    best = {"events": 0, "minutes": 0.0, "start": None}
    run_n, run_start, run_last, run_est, run_sess = 0, None, None, 0.0, None
    for kind, sess, ts, est in e.select("kind", "session", "ts_ist", EST.alias("est")).iter_rows():
        if kind == "short" and (run_n and sess == run_sess or not run_n):
            if not run_n:
                run_start = ts
            run_n, run_last, run_est, run_sess = run_n + 1, ts, est, sess
        else:
            run_n = 0
            if kind == "short":
                run_n, run_start, run_last, run_est, run_sess = 1, ts, ts, est, sess
        if run_n > best["events"]:
            best = {"events": run_n, "minutes": round(((run_last - run_start).total_seconds() + run_est) / 60, 1),
                    "start": run_start.isoformat(timespec="minutes")}
    return {
        "count": g.height,
        "median_events": median(g["events"].to_list()),
        "median_minutes": round(median(g["span_s"].to_list()) / 60, 1),
        "longest": {"start": longest["start"].isoformat(timespec="minutes"),
                    "minutes": round(longest["span_s"] / 60, 1), "events": longest["events"]},
        "longest_shorts_scroll": best,
    }


# --- rewatches, firsts, facts -----------------------------------------------------------

def _rewatches(e: pl.DataFrame) -> dict:
    v = e.sort(["video_id", "ts_utc"])
    v = v.with_columns(((pl.col("ts_utc") - pl.col("ts_utc").shift(1).over("video_id")).dt.total_seconds())
                       .alias("since"))
    v = v.with_columns((pl.col("since").is_null() | (pl.col("since") > REWATCH_GAP_S)).alias("new_watch"))
    g = v.group_by("video_id").agg(pl.col("new_watch").sum().alias("watches"),
                                   pl.col("title").drop_nulls().first().alias("title"),
                                   pl.col("channel_name").drop_nulls().first().alias("channel"),
                                   pl.col("kind").first().alias("kind"))
    again = g.filter(pl.col("watches") >= 2)
    top = again.filter(pl.col("title").is_not_null()).sort("watches", descending=True).head(5)
    return {
        "videos_watched_more_than_once": again.height,
        "extra_watches": int((again["watches"] - 1).sum() or 0),
        "top": [{"title": r["title"], "channel": r["channel"], "watches": r["watches"], "kind": r["kind"]}
                for r in top.iter_rows(named=True)],
    }


def _event_card(r: dict) -> dict:
    return {"title": r["title"], "channel": r["channel_name"], "kind": r["kind"],
            "when": r["ts_ist"].isoformat(timespec="minutes")}


def _facts(e: pl.DataFrame) -> dict:
    out: dict = {}
    titled = e.filter(pl.col("title").is_not_null())
    out["first_event"] = _event_card(titled.row(0, named=True)) if titled.height else None
    vids = titled.filter(pl.col("kind") == "video")
    out["first_video"] = _event_card(vids.row(0, named=True)) if vids.height else None
    night = e.filter(pl.col("hour") < 6)
    if night.height:
        n = night.with_columns((pl.col("hour") * 60 + pl.col("ts_ist").dt.minute()).alias("m")).sort("m", descending=True).row(0, named=True)
        out["latest_night"] = {"clock": _clock(n["ts_ist"]), "weekday": WEEKDAYS[n["wd"]], "date": _iso(n["date"])}
    else:
        out["latest_night"] = None
    day = e.group_by("date").len().sort("len", descending=True).row(0, named=True)
    out["most_events_in_a_day"] = {"date": _iso(day["date"]), "events": day["len"]}
    return out


# --- alter ego --------------------------------------------------------------------------

def _alter_ego(e: pl.DataFrame, names: dict[int, str]) -> dict | None:
    k = e.filter(pl.col("category_id").is_not_null())
    total = float(k["estimate_s"].sum() or 0.0)
    if not total:
        return None
    g = k.group_by("category_id").agg(EST.sum().alias("est")).sort("est", descending=True)
    top_id, top_est = g.row(0)
    mine = k.filter(pl.col("category_id") == top_id)
    mine_total = float(mine["estimate_s"].sum() or 0.0)
    by_slot = {s: sum(float(mine.filter(pl.col("hour") == h)["estimate_s"].sum() or 0.0) for h in hrs) for s, hrs in SLOTS.items()}
    night_share = by_slot["night"] / mine_total if mine_total else 0.0
    slot = "night" if night_share >= NIGHT_THRESHOLD else max(by_slot, key=by_slot.get)
    noun = EGO_NOUN.get(top_id, "Wanderer")
    cat = names.get(top_id, f"Category {top_id}")
    lines = [f"{cat} is {_pct(top_est, total):.0f}% of your time."]
    lines.append(f"{night_share * 100:.0f}% of it happens after midnight." if slot == "night"
                 else f"Most of it happens in the {slot}: {by_slot[slot] / mine_total * 100:.0f}% of it.")
    return {"name": f"The {SLOT_ADJ[slot]} {noun}", "category": cat, "category_share_pct": _pct(top_est, total),
            "time_of_day": slot, "evidence": lines,
            "mix": [{"category": names.get(r["category_id"], f"Category {r['category_id']}"), "share_pct": _pct(r["est"], total)}
                    for r in g.head(3).iter_rows(named=True)]}


# --- assemble ---------------------------------------------------------------------------

def _disclosures(th: dict, removed: int, q, *, lifetime: bool, partial_note: str | None) -> list[str]:
    return [d for d in [
        f"Watch time is an estimate between {th['floor']:.0f} and {th['ceiling']:.0f} hours. "
        "A video counts until your next one starts, never longer than the video itself. "
        "When nothing followed for 30+ minutes, I can't know if you finished it.",
        "Shorts are videos up to 60 seconds, plus 1-3 minute ones that say #shorts or sit between Shorts. "
        "A few will land on the wrong side.",
        f"{removed:,} watches were of videos that no longer exist, so they have no length and no time." if removed else None,
        f"{q.music_rows:,} YouTube Music plays are left out." if lifetime and q.music_rows else None,
        f"Your history starts {q.first_ist[:10]}. I can only read what Google kept." if lifetime else partial_note,
    ] if d]


def _slice(e: pl.DataFrame, a, parsed: Parsed, categories: dict[int, str], search: pl.DataFrame, *,
           lifetime: bool, partial_note: str | None = None) -> dict:
    """One report from one set of events. The lifetime report and every month use this same path."""
    s = summarize(Analysis(events=e, completion_rates=a.completion_rates, sessions=int(e["session"].n_unique())))
    total_est = float(e["estimate_s"].sum() or 0.0)
    q = parsed.quality
    subs_in_export = q.subscriptions is not None
    sub_ids = set(parsed.subscriptions["channel_id"].drop_nulls().to_list())

    kinds = {k: {**v, "events_pct": _pct(v["events"], s["events"]),
                 "time_pct": _pct(v["estimate"], s["total_hours"]["estimate"])} for k, v in s["by_kind"].items()}
    for v in kinds.values():
        for f in ("floor", "estimate", "ceiling"):
            v[f] = round(v[f], 2)

    th = {k: round(v, 2) for k, v in s["total_hours"].items()}
    removed = kinds["unknown"]["events"]
    cal = _calendar(e)
    if lifetime:
        rng = {"first": q.first_ist, "last": q.last_ist, "span_days": q.span_days, "active_days": q.active_days}
        distinct = q.distinct_videos
    else:
        rng = {"first": e["ts_ist"].min().isoformat(timespec="minutes"), "last": e["ts_ist"].max().isoformat(timespec="minutes"),
               "span_days": cal["span_days"], "active_days": cal["active_days"]}
        distinct = int(e["video_id"].n_unique())
    return {
        "schema_version": SCHEMA_VERSION,
        "range": rng,
        "totals": {"events": s["events"], "hours": th,
                   "hours_per_day": {k: round(v, 2) for k, v in s["hours_per_day"].items()},
                   "sessions": s["sessions"], "distinct_videos": distinct},
        "kinds": kinds,
        "channels": _channels(e, sub_ids, total_est),
        "categories": _categories(e, categories),
        "searches": _searches(search),
        "subscriptions": _subscriptions(e, parsed.subscriptions, subs_in_export, total_est),
        "calendar": cal,
        "rhythm": _rhythm(e, total_est),
        "sessions": _sessions(e),
        "rewatches": _rewatches(e),
        "facts": _facts(e),
        "alter_ego": _alter_ego(e, categories),
        "disclosures": _disclosures(th, removed, q, lifetime=lifetime, partial_note=partial_note),
    }


def _month_entries(e: pl.DataFrame, a, parsed: Parsed, categories: dict[int, str]) -> list[dict]:
    first, last = e["date"].min(), e["date"].max()
    keyed = e.with_columns(pl.col("ts_ist").dt.strftime("%Y-%m").alias("_m"))
    search = parsed.search
    if search.height:
        search = search.with_columns(pl.col("ts_ist").dt.strftime("%Y-%m").alias("_m"))
    out = []
    for key in sorted(keyed["_m"].unique().to_list()):
        y, m = int(key[:4]), int(key[5:])
        start = date(y, m, 1)
        end = (date(y + (m == 12), m % 12 + 1, 1) - timedelta(days=1))
        cf, cl = max(start, first), min(end, last)
        covered, in_month = (cl - cf).days + 1, end.day
        partial = covered < in_month
        note = (f"Your history only covers {cf.day} to {cl.day} {start.strftime('%B')}, so this month is partial."
                if partial else None)
        me = keyed.filter(pl.col("_m") == key).drop("_m")
        ms = search.filter(pl.col("_m") == key).drop("_m") if search.height else search
        rep = _slice(me, a, parsed, categories, ms, lifetime=False, partial_note=note)
        out.append({"key": key, "events": me.height, "hours": rep["totals"]["hours"]["estimate"],
                    "days_covered": covered, "days_in_month": in_month, "partial": partial, "report": rep})
    return out


def build_report(parsed: Parsed, meta: pl.DataFrame, categories: dict[int, str]) -> dict:
    a = build_events(parsed.watch, meta)
    e = a.events.with_columns(
        pl.col("ts_ist").dt.date().alias("date"),
        pl.col("ts_ist").dt.hour().cast(pl.Int64).alias("hour"),
        (pl.col("ts_ist").dt.weekday() - 1).cast(pl.Int64).alias("wd"),
    )
    report = _slice(e, a, parsed, categories, parsed.search, lifetime=True)
    report["months"] = _month_entries(e, a, parsed, categories)
    return report
