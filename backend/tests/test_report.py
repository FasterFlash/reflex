import json
import random
from datetime import datetime, timedelta, timezone

import polars as pl
import pytest

from fixtures import takeout, watch
from reflex.ingest import open_takeout
from reflex.parse import parse_takeout
from reflex.report import build_report, normalize_query

BASE = datetime(2026, 9, 1, 0, 0, tzinfo=timezone.utc)
CATS = {10: "Music", 20: "Gaming", 23: "Comedy", 27: "Education"}


def uc(ch):
    return "UC" + ch * 22


def ev(vid, off_s, dur, cat=27, chan=("A", "a"), title=None, hint=False):
    return dict(vid=vid, off=off_s, dur=dur, cat=cat, chan=chan, title=title or f"Title {vid}", hint=hint)


def make(events, searches=None, subs=None):
    rows, meta = [], {}
    for e in events:
        t = (BASE + timedelta(seconds=e["off"])).strftime("%Y-%m-%dT%H:%M:%S.000Z")
        rows.append(watch(e["vid"], t, title=e["title"], chan=(e["chan"][0], uc(e["chan"][1]))))
        meta[e["vid"]] = e
    search_rows = None
    if searches is not None:
        search_rows = [{"title": f"Searched for {q}", "time": (BASE + timedelta(seconds=o)).strftime("%Y-%m-%dT%H:%M:%S.000Z")}
                       for q, o in searches]
    parsed = parse_takeout(open_takeout(takeout(rows, search_rows=search_rows, subs_csv=subs)))
    m = pl.DataFrame([{"video_id": v, "meta_status": "ok", "duration_s": e["dur"], "category_id": e["cat"],
                       "api_channel_id": None, "live": "none", "shorts_hint": e["hint"]} for v, e in meta.items()],
                     schema={"video_id": pl.String, "meta_status": pl.String, "duration_s": pl.Int64,
                             "category_id": pl.Int64, "api_channel_id": pl.String, "live": pl.String,
                             "shorts_hint": pl.Boolean})
    return build_report(parsed, m, CATS)


def vid(i):
    return f"v{i:010d}"


def chained(specs, start=0):
    """Events laid end to end (each one's gap equals its length), then a 1 s Short to close the sitting."""
    out, t = [], start
    for i, (dur, kw) in enumerate(specs):
        out.append(ev(vid(len(out) + start // 7 + i * 0 + hash(str(kw)) % 1 + int(start)), t, dur, **kw))
        t += dur
    return out, t


# ---------------------------------------------------------------------------------------

def test_normalize_query():
    assert normalize_query("Lo-Fi  Beats!") == "lo fi beats"
    assert normalize_query("  LOFI beats ") == normalize_query("lofi beats")
    assert normalize_query("???") == "???"


def test_top_channel_by_time_and_by_events():
    ev_ = []
    t = 0
    # channel A: one long video; channel B: many tiny Shorts
    ev_.append(ev("a0000000001", t, 1800, chan=("Alpha", "a"))); t += 1800
    for i in range(30):
        ev_.append(ev(f"b{i:010d}", t, 20, chan=("Beta", "b"), cat=23)); t += 20
    ev_.append(ev("z0000000001", t, 5, chan=("Zed", "z")))
    r = make(ev_)
    assert r["channels"]["top_by_time"][0]["name"] == "Alpha"
    assert r["channels"]["top_by_time"][0]["hours"] == pytest.approx(0.5, abs=0.01)
    assert r["channels"]["top_by_events"][0]["name"] == "Beta"
    assert r["channels"]["top_by_events"][0]["events"] == 30
    assert r["channels"]["distinct"] == 3


def test_channels_without_info_are_counted_not_ranked():
    rows = [watch(vid(1), "2026-09-01T10:00:00.000Z", chan=None), watch(vid(2), "2026-09-01T10:01:00.000Z")]
    parsed = parse_takeout(open_takeout(takeout(rows)))
    m = pl.DataFrame([{"video_id": vid(i), "meta_status": "ok", "duration_s": 30, "category_id": 27,
                       "api_channel_id": None, "live": "none", "shorts_hint": False} for i in (1, 2)])
    r = build_report(parsed, m, CATS)
    assert r["channels"]["events_without_channel"] == 1 and r["channels"]["distinct"] == 1


def test_categories_ranked_by_time_with_names():
    e = [ev("m0000000001", 0, 600, cat=10), ev("e0000000001", 600, 60, cat=27), ev("e0000000002", 660, 3, cat=27)]
    r = make(e)
    top = r["categories"]["top_by_time"]
    assert top[0]["category"] == "Music" and top[1]["category"] == "Education"
    assert sum(c["share_of_time_pct"] for c in top) == pytest.approx(100, abs=0.5)


def test_search_grouping_and_counts():
    s = [("Lofi Beats", 0), ("lofi beats!", 100), ("LOFI  beats", 200), ("rain sounds", 300), ("lofi beats", 90000)]
    r = make([ev("x0000000001", 0, 30)], searches=s)["searches"]
    assert r["total"] == 5 and r["distinct"] == 2 and r["repeat_searches"] == 3
    assert r["first_day"] <= r["last_day"] and len(r["first_day"]) == 10
    assert r["top"][0]["times"] == 4 and r["top"][0]["on_days"] == 2
    assert r["top"][0]["query"].lower().replace("!", "").replace("  ", " ") == "lofi beats"


def test_no_search_file_is_graceful():
    assert make([ev("x0000000001", 0, 30)])["searches"] == {"total": 0, "distinct": 0, "repeat_searches": 0, "top": []}


def test_subscriptions():
    csv = f"Channel Id,Channel Url,Channel Title\n{uc('a')},u,Alpha\n{uc('b')},u,Beta\n{uc('c')},u,Gamma\n"
    e = [ev("a0000000001", 0, 600, chan=("Alpha", "a")), ev("q0000000001", 600, 600, chan=("Other", "q"))]
    s = make(e, subs=csv)["subscriptions"]
    assert s["subscribed"] == 3 and s["watched_at_least_once"] == 1 and s["never_watched"] == 2
    assert s["never_watched_pct"] == pytest.approx(66.7, abs=0.1)
    assert s["top_subscribed_by_time"][0]["name"] == "Alpha"
    assert s["top_not_subscribed_by_time"][0]["name"] == "Other"
    assert make(e)["subscriptions"] is None                     # file missing from the export


def test_streaks_gaps_and_ist_day_boundary():
    day = 86400
    # days 0,1,2 active; day 3 missed; day 4 active. The last event sits at 19:00 UTC = 00:30 IST next day.
    e = [ev(vid(i), d * day + 6 * 3600, 30) for i, d in enumerate([0, 1, 2, 4])]
    e.append(ev("late0000001", 4 * day + 19 * 3600, 30))        # 00:30 IST on day 5
    c = make(e)["calendar"]
    assert c["active_days"] == 5 and c["span_days"] == 6 and c["missed_days"] == 1
    assert c["longest_streak"]["days"] == 3
    assert c["current_streak"]["days"] == 2                      # day 4 and day 5 (IST)
    assert c["days"][-1]["date"] == "2026-09-06"                 # 2026-09-01 + 5 days
    assert sum(d["events"] for d in c["days"]) == 5


def test_rhythm_hour_and_weekday_buckets():
    # 2026-09-01 is a Tuesday. 20:30 UTC = 02:00 IST Wednesday.
    # 25 minutes watched, followed straight away by another event: same sitting, exact credit
    e = [ev("n0000000001", 20 * 3600 + 30 * 60, 1500), ev("n0000000002", 20 * 3600 + 30 * 60 + 1500, 5)]
    r = make(e)["rhythm"]
    m = r["hours_by_weekday_hour"]
    assert m[2][2] == pytest.approx(1500 / 3600, abs=0.01)       # Wednesday, 02:xx
    assert r["peak_hour"] == 2 and r["peak_weekday"] == "Wednesday"
    assert r["share_of_time_pct"]["night"] > 99


def test_rewatch_counts_distinct_watches_not_events():
    v = "r0000000001"
    e = [ev(v, 0, 200), ev(v, 300, 200),               # 5 min apart: the same watch
         ev("o0000000001", 2000, 30), ev(v, 7200, 200),  # an hour later: a second watch
         ev("o0000000002", 9000, 30), ev(v, 3 * 86400, 200)]
    r = make(e)["rewatches"]
    assert r["videos_watched_more_than_once"] == 1
    assert r["top"][0]["watches"] == 3 and r["extra_watches"] == 2


def test_longest_shorts_scroll_is_within_one_sitting():
    e, t = [], 0
    for i in range(5):
        e.append(ev(vid(i), t, 20, cat=23)); t += 20
    e.append(ev("long0000001", t, 900)); t += 900
    for i in range(3):
        e.append(ev(vid(10 + i), t, 20, cat=23)); t += 20
    r = make(e)["sessions"]["longest_shorts_scroll"]
    assert r["events"] == 5


def test_alter_ego_night_scholar_and_daytime_gamer():
    # education, watched around 01:00 IST (19:30 UTC)
    night = [ev(vid(i), 19 * 3600 + 30 * 60 + i * 600, 600, cat=27) for i in range(6)]
    night.append(ev("t0000000001", 19 * 3600 + 30 * 60 + 6 * 600, 5, cat=27))
    ego = make(night)["alter_ego"]
    assert ego["name"] == "The Night Scholar" and ego["category"] == "Education"
    assert ego["evidence"][0].startswith("Education is 100% of your time")
    assert "after midnight" in ego["evidence"][1]

    # gaming at 14:00 UTC = 19:30 IST => evening
    game = [ev(vid(i), 14 * 3600 + i * 600, 600, cat=20) for i in range(6)]
    game.append(ev("t0000000002", 14 * 3600 + 6 * 600, 5, cat=20))
    assert make(game)["alter_ego"]["name"] == "The Evening Gamer"


def test_facts_first_event_and_latest_night():
    e = [ev("f0000000001", 6 * 3600, 600, title="The very first"), ev("f0000000002", 6 * 3600 + 700, 600),
         ev("n0000000001", 21 * 3600 + 20 * 60, 30, title="late"),    # 02:50 IST
         ev("n0000000002", 22 * 3600, 30)]                            # 03:30 IST
    f = make(e)["facts"]
    assert f["first_event"]["title"] == "The very first"
    assert f["latest_night"]["clock"] == "03:30"


def test_disclosures_and_range():
    r = make([ev(vid(i), i * 100, 60) for i in range(5)])
    text = " ".join(r["disclosures"])
    assert "estimate between" in text and "Shorts are videos up to 60 seconds" in text and "Your history starts 2026-09-01" in text
    assert r["totals"]["hours"]["floor"] <= r["totals"]["hours"]["estimate"] <= r["totals"]["hours"]["ceiling"]


def test_report_is_json_and_internally_consistent_on_random_history():
    rnd = random.Random(11)
    e, t = [], 0
    chans = [(f"Ch{i}", chr(97 + i)) for i in range(12)]
    for i in range(1500):
        t += rnd.choice([4, 12, 30, 90, 600, 5000, 90000 if rnd.random() < .05 else 20])
        e.append(ev(vid(i % 900), t, rnd.choice([15, 40, 75, 120, 400, 2200, 9000]), cat=rnd.choice([10, 20, 23, 27]),
                    chan=rnd.choice(chans), hint=rnd.random() < .2))
    # duplicate video ids reuse the first metadata, as the real cache would
    first = {}
    for x in e:
        first.setdefault(x["vid"], x)
        x.update(dur=first[x["vid"]]["dur"], cat=first[x["vid"]]["cat"], hint=first[x["vid"]]["hint"])
    r = make(e, searches=[(f"q{i % 40}", i * 1000) for i in range(300)])
    json.dumps(r)                                                  # serialisable
    assert sum(k["events"] for k in r["kinds"].values()) == r["totals"]["events"]
    assert sum(d["events"] for d in r["calendar"]["days"]) == r["totals"]["events"]
    hrs = [c["hours"] for c in r["channels"]["top_by_time"]]
    assert hrs == sorted(hrs, reverse=True)
    assert sum(sum(row) for row in r["rhythm"]["hours_by_weekday_hour"]) == pytest.approx(r["totals"]["hours"]["estimate"], abs=0.5)
    assert r["calendar"]["longest_streak"]["days"] >= 1 and r["schema_version"] == 2


def _long_history():
    """About 100 days, 6 events a day, so it crosses four calendar months with partial months at both ends."""
    rnd = random.Random(5)
    e, t = [], 0
    for day in range(100):
        base = day * 86400 + 20 * 3600
        for k in range(6):
            e.append(ev(vid(day * 10 + k), base + k * 600, rnd.choice([40, 300, 900]), cat=rnd.choice([10, 23, 27]), chan=("Ch%d" % (day % 5), "c%d" % (day % 5))))
    return make(e, searches=[(f"q{i % 7}", i * 70000) for i in range(120)])


def test_months_add_up_to_lifetime():
    r = _long_history()
    json.dumps(r)
    ms = r["months"]
    assert len(ms) >= 3 and [m["key"] for m in ms] == sorted(m["key"] for m in ms)
    assert sum(m["events"] for m in ms) == r["totals"]["events"]
    assert sum(m["report"]["totals"]["events"] for m in ms) == r["totals"]["events"]
    assert sum(m["report"]["totals"]["hours"]["estimate"] for m in ms) == pytest.approx(r["totals"]["hours"]["estimate"], abs=0.1 * len(ms))
    for m in ms:
        rep = m["report"]
        assert "months" not in rep and rep["schema_version"] == 2
        assert all(d["date"][:7] == m["key"] for d in rep["calendar"]["days"])       # nothing leaks in from another month
        assert rep["range"]["first"][:7] == m["key"] and rep["range"]["last"][:7] == m["key"]
        assert all(q["first"][:7] == m["key"] and q["last"][:7] == m["key"] for q in rep["searches"]["top"])


def test_partial_months_are_flagged_and_explained():
    ms = _long_history()["months"]
    assert ms[0]["partial"] or ms[-1]["partial"]
    assert any(not m["partial"] for m in ms[1:-1]) or len(ms) < 3
    for m in ms:
        notes = " ".join(m["report"]["disclosures"])
        assert ("only covers" in notes) == m["partial"]
        assert m["days_covered"] <= m["days_in_month"]


def test_busiest_month_and_weekday():
    r = _long_history()
    bm = r["calendar"]["busiest_month"]
    assert bm["key"] in {m["key"] for m in r["months"]} and bm["days"] >= 14
    assert r["calendar"]["busiest_weekday"]["name"] in ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")
    only = r["months"][1]["report"]["calendar"]["busiest_month"]
    assert only["key"] == r["months"][1]["key"]


def test_tiny_month_does_not_crash():
    e = [ev(vid(i), i * 60, 120) for i in range(3)] + [ev(vid(99), 40 * 86400, 120)]
    r = make(e)
    assert len(r["months"]) >= 1 and all(m["report"]["totals"]["events"] > 0 for m in r["months"])
