import time

import pytest
from fastapi.testclient import TestClient

from fixtures import make_zip, takeout, messy_watch_rows
from reflex import server
from reflex.server import create_app


def fake_fetch(endpoint, params):
    if endpoint == "videoCategories":
        return {"items": [{"id": "27", "snippet": {"title": "Education"}}]}
    return {"items": [{"id": i, "contentDetails": {"duration": "PT5M"},
                       "snippet": {"title": "t", "description": "", "tags": [], "categoryId": "27",
                                   "channelId": "UC" + "a" * 22, "liveBroadcastContent": "none"}}
                      for i in params["id"].split(",")]}


@pytest.fixture
def web(tmp_path):
    w = tmp_path / "web"
    (w / "videos").mkdir(parents=True)
    for n in ("index", "upload", "report", "about"):
        (w / f"{n}.html").write_text(f"<p>{n}</p>")
    (w / "jszip.min.js").write_text("//js")
    (w / "videos" / "step1.mp4").write_bytes(b"x")
    (w / "secret.txt").write_text("nope")
    (w / "backend").mkdir()
    (w / "backend" / ".env").write_text("YOUTUBE_API_KEY=SHOULD-NEVER-BE-SERVED")
    return w


@pytest.fixture
def client(tmp_path, web):
    app = create_app(cache_path=str(tmp_path / "c.sqlite"), web_dir=web, make_fetcher=lambda: fake_fetch, workers=3)
    return TestClient(app)


def wait(client, jid, limit=20):
    t0 = time.time()
    while time.time() - t0 < limit:
        s = client.get(f"/api/jobs/{jid}").json()
        if s["state"] != "running":
            return s
        time.sleep(0.05)
    raise AssertionError("job did not finish")


def test_upload_to_report(client):
    r = client.post("/api/upload", content=takeout(messy_watch_rows()))
    assert r.status_code == 200
    jid = r.json()["id"]
    s = wait(client, jid)
    assert s["state"] == "done"
    rep = client.get(f"/api/jobs/{jid}/report")
    assert rep.status_code == 200
    body = rep.json()
    assert body["schema_version"] == 2 and body["totals"]["events"] > 0
    assert body["lookup"] == {"complete": True, "missing_videos": 0}
    assert rep.headers["cache-control"] == "no-store"


def test_report_is_handed_over_once(client):
    jid = client.post("/api/upload", content=takeout(messy_watch_rows())).json()["id"]
    wait(client, jid)
    assert client.get(f"/api/jobs/{jid}/report").status_code == 200
    assert client.get(f"/api/jobs/{jid}/report").status_code == 404
    assert client.get(f"/api/jobs/{jid}").status_code == 404


@pytest.mark.parametrize("payload,code,status", [
    (b"this is not a zip", "notzip", 422),
    (make_zip({"Takeout/other/file.txt": "hi"}), "nodata", 422),
    (make_zip({"Takeout/YouTube and YouTube Music/history/watch-history.html": "<html>"}), "html", 422),
])
def test_upload_problems_use_the_page_codes(client, payload, code, status):
    r = client.post("/api/upload", content=payload)
    assert r.status_code == status
    assert r.json()["error"]["code"] == code


def test_declared_size_over_cap_is_refused_before_reading(client, monkeypatch):
    monkeypatch.setattr(server, "MAX_ZIP_BYTES", 10)
    r = client.post("/api/upload", content=b"x" * 50)
    assert r.status_code == 413 and r.json()["error"]["code"] == "big"


def test_no_key_and_cold_cache_is_a_clean_error(tmp_path, web):
    app = create_app(cache_path=str(tmp_path / "c.sqlite"), web_dir=web, make_fetcher=lambda: None)
    c = TestClient(app)
    jid = c.post("/api/upload", content=takeout(messy_watch_rows())).json()["id"]
    s = wait(c, jid)
    assert s["state"] == "error" and s["error"]["code"] == "lookup"
    assert "key" not in s["error"]["message"].lower()


def test_unexpected_crash_does_not_leak_details(tmp_path, web):
    def boom():
        raise RuntimeError("secret internals /home/x")
    c = TestClient(create_app(cache_path=str(tmp_path / "c.sqlite"), web_dir=web, make_fetcher=boom))
    jid = c.post("/api/upload", content=takeout(messy_watch_rows())).json()["id"]
    s = wait(c, jid)
    assert s["error"]["code"] == "internal" and "secret" not in s["error"]["message"]


def test_busy_when_too_many_running(tmp_path, web, monkeypatch):
    monkeypatch.setattr(server, "MAX_RUNNING", 0)
    c = TestClient(create_app(cache_path=str(tmp_path / "c.sqlite"), web_dir=web, make_fetcher=lambda: fake_fetch))
    r = c.post("/api/upload", content=takeout(messy_watch_rows()))
    assert r.status_code == 503 and r.json()["error"]["code"] == "busy"


def test_pages_are_served_and_nothing_else(client):
    assert client.get("/").text == "<p>index</p>"
    assert client.get("/upload.html").text == "<p>upload</p>"
    assert client.get("/jszip.min.js").status_code == 200
    assert client.get("/videos/step1.mp4").status_code == 200
    for bad in ("/secret.txt", "/backend/.env", "/privacy.html", "/videos/..%2fsecret.txt",
                "/videos/step1.txt", "/..%2fsecret.txt", "/api/jobs/nope", "/docs", "/openapi.json"):
        assert client.get(bad).status_code == 404, bad


def test_unknown_job(client):
    assert client.get("/api/jobs/abc").status_code == 404
    assert client.get("/api/jobs/abc/report").status_code == 404


# ---- feedback box ----

def _fb(client, **kw):
    body = {"kind": "idea", "message": "Add a dark mode please", "contact": ""}
    body.update(kw)
    return client.post("/api/feedback", json=body)


def test_feedback_is_saved_without_ip(client, tmp_path):
    assert _fb(client, contact="me@example.com").json() == {"ok": True}
    import sqlite3
    rows = sqlite3.connect(tmp_path / "feedback.sqlite").execute("select kind, message, contact, rating from feedback").fetchall()
    assert rows == [("idea", "Add a dark mode please", "me@example.com", None)]
    cols = [r[1] for r in sqlite3.connect(tmp_path / "feedback.sqlite").execute("pragma table_info(feedback)")]
    assert cols == ["id", "ts", "kind", "message", "contact", "rating"]


@pytest.mark.parametrize("bad", [
    {"kind": "nope"}, {"kinds": []}, {"kinds": ["idea", "nope"]}, {"kinds": "idea,broke"}, {"message": "hi"}, {"message": "x" * 2001}, {"kind": None}, {"message": 5},
])
def test_feedback_rejects_bad_input(client, bad):
    assert _fb(client, **bad).status_code == 422


def test_feedback_star_rating_stands_alone(client, tmp_path):
    import sqlite3
    r = client.post("/api/feedback", json={"rating": 5})
    assert r.status_code == 200 and r.json() == {"ok": True}
    r = client.post("/api/feedback", json={"rating": 3, "message": "Nice, but slow upload", "kinds": ["idea"]})
    assert r.status_code == 200
    rows = sqlite3.connect(tmp_path / "feedback.sqlite").execute("select kind, message, rating from feedback order by id").fetchall()
    assert rows == [("", "", 5), ("idea", "Nice, but slow upload", 3)]


@pytest.mark.parametrize("bad", [{"rating": 0}, {"rating": 6}, {"rating": "5"}, {"rating": True}, {"rating": 4.5}, {"rating": 4, "message": "x" * 2001}, {"rating": 4, "kinds": ["nope"]}])
def test_feedback_rejects_bad_rating(client, bad):
    assert client.post("/api/feedback", json=bad).status_code == 422


def test_feedback_honeypot_pretends_but_stores_nothing(client, tmp_path):
    assert _fb(client, website="http://spam").json() == {"ok": True}
    import sqlite3
    assert sqlite3.connect(tmp_path / "feedback.sqlite").execute("select count(*) from feedback").fetchone()[0] == 0


def test_feedback_is_rate_limited(client):
    codes = [_fb(client).status_code for _ in range(7)]
    assert codes[:5] == [200] * 5 and codes[5:] == [429, 429]


def test_feedback_page_is_served(tmp_path):
    w = tmp_path / "w"; w.mkdir()
    for n in ("index", "feedback", "contact", "inspiration"):
        (w / f"{n}.html").write_text(n)
    c = TestClient(create_app(cache_path=str(tmp_path / "c.sqlite"), web_dir=w))
    assert [c.get(f"/{n}.html").status_code for n in ("feedback", "contact", "inspiration")] == [200, 200, 200]


def test_feedback_accepts_several_kinds(client, tmp_path):
    import sqlite3
    assert _fb(client, kinds=["idea", "broke", "idea"]).json() == {"ok": True}
    kind = sqlite3.connect(tmp_path / "feedback.sqlite").execute("select kind from feedback").fetchone()[0]
    assert kind == "broke,idea"


def test_preview_card_image_is_served_and_nothing_else_image_like(tmp_path):
    w = tmp_path / "w"; w.mkdir()
    (w / "index.html").write_text("x"); (w / "preview-card.webp").write_bytes(b"RIFFxxxxWEBP"); (w / "other.webp").write_bytes(b"no")
    c = TestClient(create_app(cache_path=str(tmp_path / "c.sqlite"), web_dir=w))
    r = c.get("/preview-card.webp")
    assert r.status_code == 200 and r.headers["content-type"] == "image/webp"
    assert c.get("/other.webp").status_code == 404
