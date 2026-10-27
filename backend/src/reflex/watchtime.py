"""Classify each watch event (Short / Video / Unsure) and estimate how long it was watched.

Watch time rests on one idea: a video was watched until the next event begins, but never
longer than the video itself. That is exact whenever another event follows soon. It is
a guess for the LAST event before a long silence ("isolated"): you may have finished it,
or left it open. On real data that guess covers about a third of total time, so it is
never hidden: every total is reported as floor <= estimate <= ceiling.

  floor     isolated events count for nothing (only time the next event proves)
  estimate  isolated events count for the average completion rate seen in the SAME
            person's own sessions, for that video length
  ceiling   isolated events count in full (min of gap and length)
"""
from __future__ import annotations

from dataclasses import dataclass

import polars as pl

SESSION_GAP_S = 30 * 60          # silence longer than this ends a session
SHORT_MAX_S = 60                 # at or under: a Short
SHORT_EXTENDED_MAX_S = 180       # Shorts can run to 3 min; over this: a Video
MIN_RATE_SAMPLES = 30
FALLBACK_RATE = 0.5

MAX_AMBIGUOUS_RUN = 3            # 61-180 s events in a row that can be pulled in by Shorts on both sides

KINDS = ["short", "video", "unknown"]


def length_bucket(dur: pl.Expr) -> pl.Expr:
    return (pl.when(dur <= 60).then(pl.lit("a <=1m"))
            .when(dur <= 180).then(pl.lit("b 1-3m"))
            .when(dur <= 600).then(pl.lit("c 3-10m"))
            .when(dur <= 1800).then(pl.lit("d 10-30m"))
            .otherwise(pl.lit("e >30m")))


def base_kind_expr() -> pl.Expr:
    """short | video | ambiguous | unknown, before context is applied.

    <=60 s            Short
    61-180 s          Short if the video says #shorts, otherwise ambiguous (resolved below)
    >180 s            Video
    no length (removed, private, live, not looked up)   unknown
    """
    d = pl.col("duration_s")
    return (pl.when(pl.col("url_is_short")).then(pl.lit("short"))
            .when(d.is_null()).then(pl.lit("unknown"))
            .when(d <= SHORT_MAX_S).then(pl.lit("short"))
            .when(d <= SHORT_EXTENDED_MAX_S).then(
                pl.when(pl.col("shorts_hint").fill_null(False)).then(pl.lit("short")).otherwise(pl.lit("ambiguous")))
            .otherwise(pl.lit("video")))


def resolve_ambiguous(kinds: list[str], isolated: list[bool]) -> list[str]:
    """Settle 61-180 s events by the scrolling context around them.

    Once someone is scrolling Shorts they keep scrolling, so a 70-90 s event with a Short
    right before it and a Short right after it, all in one sitting, is a Short. A run of up
    to MAX_AMBIGUOUS_RUN such events between two Shorts is judged together. Everything the
    context does not settle is a Video. Checked on real data: events this rule pulls in are
    swiped through like Shorts (about a third watched, ~27 s each); the rest are watched
    like clips (~80% watched).
    """
    out = list(kinds)
    n, i = len(out), 0
    while i < n:
        if kinds[i] != "ambiguous":
            i += 1
            continue
        j = i
        while j < n and kinds[j] == "ambiguous":
            j += 1
        # same sitting: no long silence between the Short before, the run, and the Short after
        framed = (i > 0 and j < n and kinds[i - 1] == "short" and kinds[j] == "short"
                  and not any(isolated[i - 1:j]))
        verdict = "short" if framed and (j - i) <= MAX_AMBIGUOUS_RUN else "video"
        for k in range(i, j):
            out[k] = verdict
        i = j
    return out


@dataclass
class Analysis:
    events: pl.DataFrame
    completion_rates: dict[str, float]      # per length bucket, from this person's own sessions
    sessions: int


def build_events(watch: pl.DataFrame, meta: pl.DataFrame) -> Analysis:
    """watch: parse_takeout().watch. meta: enrich.meta_frame()."""
    d = (watch.filter(~pl.col("is_music"))
         .join(meta, on="video_id", how="left")
         .sort(["ts_utc", "src_idx"], descending=[False, True]))

    gap = ((pl.col("ts_utc").shift(-1) - pl.col("ts_utc")).dt.total_milliseconds() / 1000.0)
    d = d.with_columns(gap.alias("gap_s"), base_kind_expr().alias("kind"))
    d = d.with_columns(
        (pl.col("gap_s").is_null() | (pl.col("gap_s") > SESSION_GAP_S)).alias("isolated"),
        length_bucket(pl.col("duration_s")).alias("bucket"),
    )
    d = d.with_columns(pl.Series("kind", resolve_ambiguous(d["kind"].to_list(), d["isolated"].to_list())))
    # a new session starts on the event after an isolated one
    d = d.with_columns(pl.col("isolated").shift(1).fill_null(False).cast(pl.Int64).cum_sum().alias("session"))

    dur = pl.col("duration_s").cast(pl.Float64)
    timed = pl.col("duration_s").is_not_null()
    ceiling = pl.when(timed).then(pl.min_horizontal(pl.col("gap_s").fill_null(dur), dur)).otherwise(None)
    d = d.with_columns(ceiling.alias("ceiling_s"))

    # completion rate for isolated events: learned from this person's non-isolated events
    inside = d.filter(timed & ~pl.col("isolated"))
    inside = inside.with_columns((pl.col("ceiling_s") / pl.col("duration_s")).alias("frac"))
    overall = inside["frac"].mean() if inside.height else FALLBACK_RATE
    per = inside.group_by("bucket").agg(pl.len().alias("n"), pl.col("frac").mean().alias("rate"))
    rates = {r["bucket"]: (r["rate"] if r["n"] >= MIN_RATE_SAMPLES else overall) for r in per.iter_rows(named=True)}
    for b in ("a <=1m", "b 1-3m", "c 3-10m", "d 10-30m", "e >30m"):
        rates.setdefault(b, overall)

    rate_df = pl.DataFrame({"bucket": list(rates), "rate": list(rates.values())})
    d = d.join(rate_df, on="bucket", how="left")
    d = d.with_columns(
        pl.when(~timed).then(None).when(pl.col("isolated")).then(0.0).otherwise(pl.col("ceiling_s")).alias("floor_s"),
        pl.when(~timed).then(None).when(pl.col("isolated")).then(dur * pl.col("rate")).otherwise(pl.col("ceiling_s")).alias("estimate_s"),
    ).drop("rate")
    d = d.sort(["ts_utc", "src_idx"], descending=[False, True])
    return Analysis(events=d, completion_rates=rates, sessions=int(d["session"].max() + 1) if d.height else 0)


def hours(df: pl.DataFrame) -> dict[str, float]:
    return {k: float(df[f"{k}_s"].sum() or 0.0) / 3600 for k in ("floor", "estimate", "ceiling")}


def summarize(a: Analysis) -> dict:
    e = a.events
    by_kind = {}
    for k in KINDS:
        sub = e.filter(pl.col("kind") == k)
        by_kind[k] = {"events": sub.height, **hours(sub)}
    days = e["ts_ist"].dt.date().n_unique()
    total = hours(e)
    isolated = e.filter(pl.col("isolated") & pl.col("duration_s").is_not_null())
    return {
        "events": e.height,
        "days": days,
        "sessions": a.sessions,
        "total_hours": total,
        "hours_per_day": {k: v / days for k, v in total.items()} if days else {},
        "by_kind": by_kind,
        "isolated": {"events": isolated.height,
                     "share_of_ceiling": (hours(isolated)["ceiling"] / total["ceiling"]) if total["ceiling"] else 0.0},
        "completion_rates": a.completion_rates,
    }
