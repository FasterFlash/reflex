import json

import polars as pl
import pytest

from fixtures import messy_watch_rows, takeout, watch
from reflex.errors import NoYouTubeData
from reflex.ingest import open_takeout
from reflex.parse import channel_id_from_url, clean_text, parse_takeout, video_id_from_url


@pytest.fixture(scope="module")
def parsed():
    return parse_takeout(open_takeout(takeout(messy_watch_rows(), search_rows=[], subs_csv="")))


def test_drop_reasons_are_counted(parsed):
    d = parsed.quality.dropped
    assert d["post"] == 1 and d["ad"] == 1 and d["duplicate"] == 1
    assert d["bad_time"] == 1 and d["no_video_id"] == 1 and d["feature_use"] == 1


def test_every_input_row_is_accounted_for(parsed):
    q = parsed.quality
    assert q.watch_rows_kept + sum(q.dropped.values()) == q.watch_rows_in


def test_kept_rows(parsed):
    # 17 in; dropped: post, ad, duplicate, bad time, no url, "Used Shorts"
    assert parsed.watch.height == 11


def test_chronological_order(parsed):
    ts = parsed.watch["ts_utc"].to_list()
    assert ts == sorted(ts)


def test_tied_timestamps_use_file_order(parsed):
    ids = parsed.watch["video_id"].to_list()
    assert ids.index("ddddddddddd") < ids.index("ccccccccccc")     # later in file = earlier watch
    assert parsed.quality.same_timestamp_rows == 2


def test_ist_day_boundary(parsed):
    w = parsed.watch.filter(pl.col("video_id").is_in(["aaaaaaaaaaa", "bbbbbbbbbbb"]))
    got = dict(zip(w["video_id"], w["ts_ist"].dt.strftime("%Y-%m-%d %H:%M")))
    assert got["bbbbbbbbbbb"] == "2026-10-16 23:50"
    assert got["aaaaaaaaaaa"] == "2026-10-17 00:20"


def test_removed_video_has_no_title(parsed):
    r = parsed.watch.filter(pl.col("video_id") == "fffffffffff").row(0, named=True)
    assert r["title"] is None and r["title_removed"] is True
    assert parsed.quality.removed_title_rows == 1


def test_missing_channel(parsed):
    assert parsed.quality.missing_channel_rows == 2          # eeee... and the removed video
    r = parsed.watch.filter(pl.col("video_id") == "eeeeeeeeeee").row(0, named=True)
    assert r["channel_key"] is None


def test_mojibake_and_whitespace_repaired(parsed):
    r = parsed.watch.filter(pl.col("video_id") == "jjjjjjjjjjj").row(0, named=True)
    assert r["title"] == "Café — mojibake"
    assert r["channel_name"] == "Spaced Out"


def test_non_english_and_emoji_untouched(parsed):
    r = parsed.watch.filter(pl.col("video_id") == "kkkkkkkkkkk").row(0, named=True)
    assert r["title"] == "日本語 \U0001F3B5"
    assert r["channel_name"].endswith("\U0001F3B5")


def test_shorts_url_is_flagged(parsed):
    r = parsed.watch.filter(pl.col("video_id") == "lllllllllll").row(0, named=True)
    assert r["url_is_short"] is True


def test_channels_group_by_id_not_name():
    rows = [
        watch("aaaaaaaaaaa", "2026-01-01T10:00:00Z", chan=("Old Name", "UC" + "c" * 22)),
        watch("bbbbbbbbbbb", "2026-01-02T10:00:00Z", chan=("New Name", "UC" + "c" * 22)),
    ]
    p = parse_takeout(open_takeout(takeout(rows)))
    assert p.quality.distinct_channels == 1


def test_range_and_active_days(parsed):
    q = parsed.quality
    assert q.first_ist.startswith("2026-10-15") and q.last_ist.startswith("2026-10-17")
    assert q.span_days == 3 and q.active_days == 3


def test_search_parsing():
    search = [
        {"title": "Searched for lofi beats", "time": "2026-10-15T10:00:00.000Z"},
        {"title": "Searched for lofi beats", "time": "2026-10-15T09:00:00.000Z"},
        {"title": "Visited https://example.com", "time": "2026-10-15T08:00:00.000Z"},
        {"title": "Searched for ", "time": "2026-10-15T07:00:00.000Z"},
    ]
    p = parse_takeout(open_takeout(takeout(messy_watch_rows(), search_rows=search)))
    assert p.search.height == 2
    assert p.quality.search_dropped["not_a_search"] == 1 and p.quality.search_dropped["empty"] == 1
    assert p.search["query"].to_list() == ["lofi beats", "lofi beats"]


def test_subscriptions_parsing_handles_bom_and_blank_rows():
    csv_text = "﻿Channel ID,Channel URL,Channel title\nUCaaaaaaaaaaaaaaaaaaaaaa,http://www.youtube.com/channel/UCaaaaaaaaaaaaaaaaaaaaaa,Chan\n,,\n"
    p = parse_takeout(open_takeout(takeout(messy_watch_rows(), subs_csv=csv_text)))
    assert p.subscriptions.height == 1 and p.quality.subscriptions == 1


def test_missing_subscriptions_is_none_not_zero():
    p = parse_takeout(open_takeout(takeout(messy_watch_rows())))
    assert p.quality.subscriptions is None


def test_empty_history_is_nodata():
    with pytest.raises(NoYouTubeData):
        parse_takeout(open_takeout(takeout([])))


def test_invalid_json_is_nodata():
    from fixtures import BASE, make_zip
    z = make_zip({BASE + "history/watch-history.json": "{not json"})
    with pytest.raises(NoYouTubeData):
        parse_takeout(open_takeout(z))


@pytest.mark.parametrize("url,expected", [
    ("https://www.youtube.com/watch?v=az41t0wUsrI", ("az41t0wUsrI", False)),
    ("https://www.youtube.com/watch?v=az41t0wUsrI&t=30s", ("az41t0wUsrI", False)),
    ("https://youtu.be/az41t0wUsrI", ("az41t0wUsrI", False)),
    ("https://www.youtube.com/shorts/az41t0wUsrI", ("az41t0wUsrI", True)),
    ("https://www.youtube.com/watch?v=short", (None, False)),
    ("https://example.com/watch?v=az41t0wUsrI", (None, False)),
    (None, (None, False)),
])
def test_video_id_from_url(url, expected):
    assert video_id_from_url(url) == expected


def test_channel_id_from_url():
    assert channel_id_from_url("https://www.youtube.com/channel/UC" + "a" * 22) == "UC" + "a" * 22
    assert channel_id_from_url("https://www.youtube.com/@handle") is None


def test_clean_text_keeps_curly_quotes():
    assert clean_text("it’s  fine ") == "it’s fine"


def test_music_is_flagged_not_dropped(parsed):
    m = parsed.watch.filter(pl.col("is_music"))
    assert m["video_id"].to_list() == ["mmmmmmmmmmm"]
    assert parsed.quality.music_rows == 1


def test_distinct_counts_exclude_music(parsed):
    # 11 kept rows, 1 music; distinct videos exclude it
    assert parsed.quality.distinct_videos == 10


def test_music_detected_by_url_alone():
    rows = [watch("aaaaaaaaaaa", "2026-01-01T10:00:00Z")]
    rows[0]["titleUrl"] = "https://music.youtube.com/watch?v=aaaaaaaaaaa"
    assert parse_takeout(open_takeout(takeout(rows))).quality.music_rows == 1
