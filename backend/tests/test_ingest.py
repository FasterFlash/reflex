import io
import zipfile

import pytest

from fixtures import BASE, make_zip, messy_watch_rows, takeout
from reflex.errors import HtmlExport, NoYouTubeData, NotAZip, TooBig
from reflex.ingest import MAX_ZIP_BYTES, open_takeout


def test_finds_all_three_files():
    z = takeout(messy_watch_rows(), search_rows=[], subs_csv="Channel ID,Channel URL,Channel title\n")
    f = open_takeout(z)
    assert f.watch and f.search is not None and f.subscriptions is not None


def test_search_and_subs_are_optional():
    f = open_takeout(takeout(messy_watch_rows()))
    assert f.search is None and f.subscriptions is None


def test_folder_name_does_not_matter():
    z = takeout(messy_watch_rows(), folder="Takeout/YouTube et YouTube Music/")
    assert open_takeout(z).watch


def test_not_a_zip():
    with pytest.raises(NotAZip):
        open_takeout(b"this is just text")


def test_html_export_is_named():
    z = make_zip({BASE + "history/watch-history.html": "<html></html>"})
    with pytest.raises(HtmlExport):
        open_takeout(z)


def test_zip_without_youtube():
    with pytest.raises(NoYouTubeData):
        open_takeout(make_zip({"Takeout/Drive/a.txt": "x"}))


def test_too_big_is_refused_before_opening():
    with pytest.raises(TooBig):
        open_takeout(b"\0" * (MAX_ZIP_BYTES + 1))


def test_path_traversal_is_refused():
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("../evil.txt", "x")
        z.writestr(BASE + "history/watch-history.json", "[]")
    with pytest.raises(NotAZip):
        open_takeout(buf.getvalue())


def test_zip_bomb_ratio_is_refused():
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr(BASE + "history/watch-history.json", "[]")
        z.writestr("bomb.bin", b"\0" * (50 * 1024 * 1024))     # compresses ~1000:1
    with pytest.raises(NotAZip):
        open_takeout(buf.getvalue())
