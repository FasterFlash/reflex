import random
from datetime import datetime, timedelta, timezone

import polars as pl
import pytest

from reflex.watchtime import (FALLBACK_RATE, MIN_RATE_SAMPLES, SESSION_GAP_S, build_events, summarize)

T0 = datetime(2026, 10, 1, 16, 0, tzinfo=timezone.utc)


def frames(rows):
    """rows: (offset_s, duration_s, extras). Returns (watch, meta) shaped like the real pipeline."""
    watch, meta = [], []
    for i, (off, dur, x) in enumerate(rows):
        vid = f"v{i:010d}"
        watch.append({"src_idx": i, "video_id": vid, "url_is_short": x.get("short_url", False),
                      "is_music": x.get("music", False), "ts_utc": T0 + timedelta(seconds=off)})
        meta.append({"video_id": vid, "meta_status": "ok" if dur is not None else "unavailable",
                     "duration_s": dur, "shorts_hint": x.get("hint", False)})
    w = pl.DataFrame(watch, schema={"src_idx": pl.Int64, "video_id": pl.String, "url_is_short": pl.Boolean,
                                    "is_music": pl.Boolean, "ts_utc": pl.Datetime("us", "UTC")})
    w = w.with_columns(pl.col("ts_utc").dt.convert_time_zone("Asia/Kolkata").alias("ts_ist"))
    m = pl.DataFrame(meta, schema={"video_id": pl.String, "meta_status": pl.String,
                                   "duration_s": pl.Int64, "shorts_hint": pl.Boolean})
    return w, m


def run(rows):
    return build_events(*frames(rows))


def col(a, name):
    return a.events[name].to_list()


def test_long_video_abandoned_for_shorts_counts_only_time_actually_spent():
    # the 8-hour video case: starts 16:00, next event 25 minutes later
    a = run([(0, 8 * 3600, {}), (25 * 60, 30, {})])
    assert col(a, "ceiling_s")[0] == 25 * 60
    assert col(a, "floor_s")[0] == 25 * 60 and col(a, "estimate_s")[0] == 25 * 60   # in-session: exact


def test_short_swiped_early_counts_the_gap_not_the_length():
    a = run([(0, 20, {}), (5, 20, {}), (65, 20, {})])
    assert col(a, "ceiling_s")[:2] == [5, 20]           # swiped after 5 s; then watched fully, 60 s passed


def test_isolated_event_is_the_uncertain_one():
    a = run([(0, 600, {}), (SESSION_GAP_S + 3 * 3600, 30, {})])
    first = a.events.row(0, named=True)
    assert first["isolated"] is True
    assert first["floor_s"] == 0 and first["ceiling_s"] == 600
    assert 0 <= first["estimate_s"] <= 600


def test_last_event_of_the_file_is_isolated():
    a = run([(0, 100, {}), (60, 100, {})])
    assert col(a, "isolated") == [False, True]


def test_exactly_at_the_session_gap_is_still_in_session():
    a = run([(0, 3600, {}), (SESSION_GAP_S, 30, {})])
    assert col(a, "isolated")[0] is False
    a = run([(0, 3600, {}), (SESSION_GAP_S + 1, 30, {})])
    assert col(a, "isolated")[0] is True


def test_sessions_are_numbered_after_each_silence():
    a = run([(0, 30, {}), (10, 30, {}), (4 * 3600, 30, {}), (4 * 3600 + 10, 30, {}), (9 * 3600, 30, {})])
    assert col(a, "session") == [0, 0, 1, 1, 2] and a.sessions == 3


@pytest.mark.parametrize("dur,hint,url,expected", [
    (15, False, False, "short"), (60, False, False, "short"),
    (61, False, False, "video"), (90, True, False, "short"), (180, False, False, "video"), (180, True, False, "short"),
    (181, False, False, "video"), (181, True, False, "video"), (3600, False, False, "video"),
    (500, False, True, "short"), (None, False, False, "unknown"),
])
def test_kind_rules(dur, hint, url, expected):
    a = run([(0, dur, {"hint": hint, "short_url": url})])
    assert col(a, "kind") == [expected]


def seq(durs, step=20):
    return [(i * step, d, {}) for i, d in enumerate(durs)]


def kinds(durs, **kw):
    return col(run(seq(durs, **kw)), "kind")


def test_scrolling_example_from_the_design_discussion():
    # 12 30 30 40 [80] 40 30 [70] 10 45  ->  the 80 s and 70 s events are Shorts
    k = kinds([12, 30, 30, 40, 80, 40, 30, 70, 10, 45])
    assert k[4] == "short" and k[7] == "short" and set(k) == {"short"}


def test_clip_between_videos_stays_a_video():
    assert kinds([600, 90, 600, 30])[1] == "video"


def test_short_on_one_side_only_is_not_enough():
    assert kinds([30, 90, 600, 30])[1] == "video"
    assert kinds([600, 90, 30, 30])[1] == "video"


def test_first_and_last_events_have_no_context():
    assert kinds([90, 30, 30])[0] == "video"
    assert kinds([30, 30, 90])[2] == "video"


def test_a_run_of_ambiguous_events_is_judged_together():
    assert kinds([30, 80, 90, 30]) [1:3] == ["short", "short"]
    assert kinds([30, 80, 90, 100, 30])[1:4] == ["short"] * 3


def test_a_long_run_is_not_pulled_in():
    assert kinds([30, 80, 90, 100, 110, 30])[1:5] == ["video"] * 4


def test_silence_breaks_the_context():
    # the 90 s event is separated from its Shorts by hours: different sittings
    rows = [(0, 30, {}), (5, 30, {}), (5 + 3 * 3600, 90, {}), (5 + 3 * 3600 + 20, 30, {}), (5 + 3 * 3600 + 40, 30, {})]
    assert col(run(rows), "kind")[2] == "video"
    rows = [(0, 30, {}), (20, 30, {}), (40, 90, {}), (40 + 3 * 3600, 30, {}), (40 + 3 * 3600 + 20, 30, {})]
    assert col(run(rows), "kind")[2] == "video"


def test_removed_neighbour_blocks_context():
    assert kinds([30, 90, None, 30])[1] == "video"


def test_hint_does_not_need_context():
    a = run([(0, 600, {}), (20, 90, {"hint": True}), (40, 600, {})])
    assert col(a, "kind")[1] == "short"


def test_there_is_no_unsure_kind():
    rnd = random.Random(5)
    rows = [(i * 15, rnd.choice([20, 70, 100, 400, None]), {}) for i in range(400)]
    assert set(col(run(rows), "kind")) <= {"short", "video", "unknown"}


def test_unknown_length_has_no_credit_but_still_counts_as_the_next_event():
    a = run([(0, 600, {}), (100, None, {}), (200, 30, {})])
    assert col(a, "kind")[1] == "unknown"
    assert col(a, "ceiling_s")[1] is None and col(a, "estimate_s")[1] is None
    assert col(a, "ceiling_s")[0] == 100                 # the removed video still ended the first one


def test_music_is_excluded():
    a = run([(0, 200, {"music": True}), (10, 200, {})])
    assert a.events.height == 1


def test_tied_timestamps_do_not_double_count():
    a = run([(0, 100, {}), (0, 100, {}), (60, 100, {})])
    assert sorted(col(a, "ceiling_s")[:2]) == [0, 60]


def test_estimate_uses_this_persons_own_completion_rate():
    # 40 in-session 10-minute videos, each abandoned after 60 s (rate = 10%), then one isolated 10-minute video
    rows = [(i * 60, 600, {}) for i in range(40)]
    rows.append((40 * 60 + 5 * 3600, 600, {}))
    a = run(rows)
    iso = a.events.filter(pl.col("isolated")).row(0, named=True)
    assert a.completion_rates["c 3-10m"] == pytest.approx(0.1, abs=1e-9)
    assert iso["estimate_s"] == pytest.approx(600 * a.completion_rates["c 3-10m"])
    assert iso["floor_s"] == 0 and iso["ceiling_s"] == 600


def test_thin_buckets_fall_back_to_the_overall_rate():
    rows = [(i * 30, 30, {}) for i in range(MIN_RATE_SAMPLES + 5)] + [(10 ** 6, 1200, {})]
    a = run(rows)
    assert a.completion_rates["d 10-30m"] == pytest.approx(a.completion_rates["a <=1m"], abs=0.5)


def test_no_in_session_data_uses_fallback():
    a = run([(0, 100, {})])
    assert a.completion_rates["a <=1m"] == FALLBACK_RATE


def test_floor_never_exceeds_estimate_never_exceeds_ceiling():
    rnd = random.Random(7)
    rows, t = [], 0
    for _ in range(600):
        t += rnd.choice([3, 10, 40, 300, 2000, 20000])
        rows.append((t, rnd.choice([15, 45, 90, 400, 2500, 14000, None]), {"hint": rnd.random() < .3}))
    e = run(rows).events.filter(pl.col("duration_s").is_not_null())
    assert (e["floor_s"] <= e["estimate_s"] + 1e-9).all() and (e["estimate_s"] <= e["ceiling_s"] + 1e-9).all()
    assert (e["ceiling_s"] <= e["duration_s"] + 1e-9).all()


def test_summary_adds_up():
    rnd = random.Random(3)
    rows, t = [], 0
    for _ in range(300):
        t += rnd.choice([5, 20, 90, 4000])
        rows.append((t, rnd.choice([20, 50, 100, 300, 2000, None]), {}))
    s = summarize(run(rows))
    assert sum(v["events"] for v in s["by_kind"].values()) == s["events"]
    for k in ("floor", "estimate", "ceiling"):
        assert sum(v[k] for v in s["by_kind"].values()) == pytest.approx(s["total_hours"][k])
    assert s["total_hours"]["floor"] <= s["total_hours"]["estimate"] <= s["total_hours"]["ceiling"]
