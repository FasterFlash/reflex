import urllib.error
from datetime import datetime, timedelta, timezone

import pytest

from reflex import enrich as E
from reflex.enrich import (BATCH, BadKey, EnrichError, MetaCache, QuotaExceeded, VideoMeta, enrich,
                           fetch_categories, http_fetcher, meta_from_item, parse_duration, plan)


def ids(n, prefix="v"):
    return [f"{prefix}{i:010d}" for i in range(n)]


class FakeAPI:
    """Stands in for the YouTube API. Ids in `missing` are not returned (removed/private)."""

    def __init__(self, missing=(), quota_after=None):
        self.calls, self.missing, self.quota_after = [], set(missing), quota_after

    def __call__(self, endpoint, params):
        if self.quota_after is not None and len(self.calls) >= self.quota_after:
            raise QuotaExceeded("daily API quota used up")
        self.calls.append((endpoint, params))
        if endpoint == "videoCategories":
            return {"items": [{"id": "10", "snippet": {"title": "Music"}}, {"id": "27", "snippet": {"title": "Education"}}]}
        return {"items": [
            {"id": v, "contentDetails": {"duration": "PT1M5S"},
             "snippet": {"categoryId": "27", "channelId": "UCx", "liveBroadcastContent": "none",
                         "title": "t", "description": "d", "tags": []}}
            for v in params["id"].split(",") if v not in self.missing]}


@pytest.mark.parametrize("raw,secs", [
    ("PT45S", 45), ("PT1M", 60), ("PT1H2M3S", 3723), ("PT10M", 600), ("P1DT2H", 93600),
    ("P0D", None), ("PT0S", None), ("", None), (None, None), ("garbage", None),
])
def test_parse_duration(raw, secs):
    assert parse_duration(raw) == secs


def test_shorts_hint_from_title_description_or_tags():
    base = {"id": "a", "contentDetails": {"duration": "PT30S"}, "snippet": {"categoryId": "1"}}
    for field, val in [("title", "Fun #Shorts"), ("description", "x #shorts"), ("tags", ["Shorts"])]:
        item = {**base, "snippet": {**base["snippet"], field: val}}
        assert meta_from_item(item).shorts_hint is True
    assert meta_from_item(base).shorts_hint is False


def test_batches_of_50_and_everything_cached():
    api, cache = FakeAPI(), MetaCache()
    stats = enrich(ids(120), cache, api)
    assert [len(c[1]["id"].split(",")) for c in api.calls] == [50, 50, 20]
    assert stats.calls == 3 and stats.fetched_ok == 120 and cache.count() == 120


def test_second_run_costs_nothing():
    api, cache = FakeAPI(), MetaCache()
    enrich(ids(60), cache, api)
    api2 = FakeAPI()
    stats = enrich(ids(60), cache, api2)
    assert api2.calls == [] and stats.cache_hits == 60 and stats.calls == 0


def test_only_new_ids_are_fetched():
    api, cache = FakeAPI(), MetaCache()
    enrich(ids(10), cache, api)
    api2 = FakeAPI()
    enrich(ids(15), cache, api2)
    assert len(api2.calls) == 1 and len(api2.calls[0][1]["id"].split(",")) == 5


def test_duplicates_in_input_are_collapsed():
    api = FakeAPI()
    stats = enrich(ids(5) * 4, MetaCache(), api)
    assert stats.requested == 5 and len(api.calls) == 1


def test_missing_videos_are_recorded_as_unavailable_and_not_refetched():
    cache = MetaCache()
    bad = ids(3)[:1]
    stats = enrich(ids(3), cache, FakeAPI(missing=bad))
    assert stats.unavailable == 1 and stats.fetched_ok == 2
    assert cache.get_many(bad)[bad[0]].status == "unavailable"
    api2 = FakeAPI()
    enrich(ids(3), cache, api2)
    assert api2.calls == []


def test_stale_unavailable_is_retried():
    cache = MetaCache()
    old = (datetime.now(timezone.utc) - timedelta(days=E.UNAVAILABLE_RETRY_DAYS + 1)).isoformat(timespec="seconds")
    cache.put_many([VideoMeta("v0000000000", "unavailable", fetched_at=old)])
    todo, hits = plan(["v0000000000"], cache)
    assert todo == ["v0000000000"] and hits == 0


def test_quota_stops_cleanly_and_keeps_progress():
    cache = MetaCache()
    stats = enrich(ids(200), cache, FakeAPI(quota_after=2))
    assert stats.stopped == "quota" and stats.calls == 2
    assert cache.count() == 100 and stats.remaining == 100
    stats2 = enrich(ids(200), cache, FakeAPI())               # tomorrow: only the rest
    assert stats2.calls == 2 and cache.count() == 200


def test_categories_fetched_once():
    api, cache = FakeAPI(), MetaCache()
    assert fetch_categories(cache, api) == {10: "Music", 27: "Education"}
    fetch_categories(cache, api)
    assert len(api.calls) == 1


def test_cache_persists_on_disk(tmp_path):
    p = tmp_path / "sub" / "c.sqlite"
    enrich(ids(5), MetaCache(p), FakeAPI())
    assert MetaCache(p).count() == 5


def test_cache_holds_no_user_history_columns():
    cols = {r[1] for r in MetaCache().db.execute("PRAGMA table_info(video_meta)")}
    assert cols == {"video_id", "status", "duration_s", "category_id", "channel_id", "live", "shorts_hint", "fetched_at"}


# --- real HTTP layer, with urllib mocked -------------------------------------------------

KEY = "AIzaSy-SECRET-KEY-123"


class _Err(urllib.error.HTTPError):
    def __init__(self, code, reason):
        import io, json
        body = json.dumps({"error": {"errors": [{"reason": reason}]}}).encode()
        super().__init__("https://x/?key=" + KEY, code, "err", {}, io.BytesIO(body))


def _patch(monkeypatch, outcomes):
    seq = iter(outcomes)
    seen = {}

    def fake_urlopen(req, timeout=None):
        seen["req"] = req
        o = next(seq)
        if isinstance(o, Exception):
            raise o
        import io, json
        return io.BytesIO(json.dumps(o).encode())

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    return seen


def test_key_goes_in_header_not_url(monkeypatch):
    seen = _patch(monkeypatch, [{"items": []}])
    http_fetcher(KEY)("videos", {"part": "snippet", "id": "a,b"})
    assert KEY not in seen["req"].full_url
    assert seen["req"].get_header("X-goog-api-key") == KEY


def test_quota_error_is_named_and_leaks_nothing(monkeypatch):
    _patch(monkeypatch, [_Err(403, "quotaExceeded")])
    with pytest.raises(QuotaExceeded) as e:
        http_fetcher(KEY)("videos", {})
    assert KEY not in str(e.value) and KEY not in repr(e.value.__cause__)


def test_bad_key_is_named_and_leaks_nothing(monkeypatch):
    _patch(monkeypatch, [_Err(400, "keyInvalid")])
    with pytest.raises(BadKey) as e:
        http_fetcher(KEY)("videos", {})
    assert KEY not in str(e.value)


def test_transient_errors_are_retried(monkeypatch):
    _patch(monkeypatch, [_Err(503, "backendError"), urllib.error.URLError("boom"), {"items": [1]}])
    out = http_fetcher(KEY, sleep=lambda s: None)("videos", {})
    assert out == {"items": [1]}


def test_gives_up_after_retries_without_leaking(monkeypatch):
    _patch(monkeypatch, [_Err(500, "x")] * 5)
    with pytest.raises(EnrichError) as e:
        http_fetcher(KEY, retries=2, sleep=lambda s: None)("videos", {})
    assert KEY not in str(e.value) and "500" in str(e.value)


def test_rotating_fetcher_moves_to_next_key_on_quota(monkeypatch):
    calls = []

    def fake_http(key, **kw):
        def f(endpoint, params):
            calls.append(key)
            if key == "A":
                raise QuotaExceeded("used up")
            return {"ok": key}
        return f

    monkeypatch.setattr(E, "http_fetcher", fake_http)
    fetch = E.rotating_fetcher(["A", "B"])
    assert fetch("videos", {}) == {"ok": "B"}
    assert fetch("videos", {}) == {"ok": "B"}
    assert calls == ["A", "B", "B"]          # spent key is skipped, not retried every call


def test_rotating_fetcher_raises_when_all_spent_and_retries_after_cooldown(monkeypatch):
    now = [0.0]
    state = {"A": False}

    def fake_http(key, **kw):
        def f(endpoint, params):
            if state[key] is False:
                raise QuotaExceeded("used up")
            return {"ok": key}
        return f

    monkeypatch.setattr(E, "http_fetcher", fake_http)
    fetch = E.rotating_fetcher(["A"], cooldown_s=100, clock=lambda: now[0])
    with pytest.raises(QuotaExceeded):
        fetch("videos", {})
    with pytest.raises(QuotaExceeded):       # still cooling down, no call made
        fetch("videos", {})
    state["A"] = True
    now[0] = 101
    assert fetch("videos", {}) == {"ok": "A"}


def test_rotating_fetcher_drops_rejected_key_for_good(monkeypatch):
    def fake_http(key, **kw):
        def f(endpoint, params):
            if key == "BAD":
                raise BadKey("rejected")
            return {"ok": key}
        return f

    monkeypatch.setattr(E, "http_fetcher", fake_http)
    fetch = E.rotating_fetcher(["BAD", "GOOD"])
    assert fetch("videos", {}) == {"ok": "GOOD"}


def test_youtube_keys_reads_numbered_vars(tmp_path):
    from reflex.config import youtube_keys
    env = tmp_path / ".env"
    env.write_text("YOUTUBE_API_KEY=k0\nYOUTUBE_API_KEY_1=k1\nYOUTUBE_API_KEY_2=k0\nYOUTUBE_API_KEY_3=k3\n")
    assert youtube_keys(env) == ["k0", "k1", "k3"]
