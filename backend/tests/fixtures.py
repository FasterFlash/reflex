"""Build Takeout-shaped ZIPs with the messiness real exports have."""
from __future__ import annotations

import io
import json
import zipfile

BASE = "Takeout/YouTube and YouTube Music/"
UC = "UC" + "a" * 22
UC2 = "UC" + "b" * 22


def watch(vid, t, title="Some video", chan=("Chan", UC), **extra):
    r = {
        "header": "YouTube",
        "title": f"Watched {title}",
        "titleUrl": f"https://www.youtube.com/watch?v={vid}",
        "time": t,
        "products": ["YouTube"],
        "activityControls": ["YouTube watch history"],
    }
    if chan:
        r["subtitles"] = [{"name": chan[0], "url": f"https://www.youtube.com/channel/{chan[1]}"}]
    r.update(extra)
    return r


def make_zip(files: dict[str, bytes | str]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for name, data in files.items():
            z.writestr(name, data)
    return buf.getvalue()


def takeout(watch_rows, search_rows=None, subs_csv=None, folder=BASE) -> bytes:
    files = {folder + "history/watch-history.json": json.dumps(watch_rows)}
    if search_rows is not None:
        files[folder + "history/search-history.json"] = json.dumps(search_rows)
    if subs_csv is not None:
        files[folder + "subscriptions/subscriptions.csv"] = subs_csv
    return make_zip(files)


def messy_watch_rows():
    """One row per edge case. Listed newest-first, like Takeout."""
    return [
        watch("aaaaaaaaaaa", "2026-10-16T18:50:00.000Z"),                       # 00:20 IST, next day
        watch("bbbbbbbbbbb", "2026-10-16T18:20:00.000Z"),                       # 23:50 IST
        watch("ccccccccccc", "2026-10-16T10:00:00.000Z", title="Later of tie"),     # tie: file-first => later
        watch("ddddddddddd", "2026-10-16T10:00:00.000Z", title="Earlier of tie"),   # tie: file-last => earlier
        watch("eeeeeeeeeee", "2026-10-15T09:00:00.000Z", chan=None),            # no subtitles
        watch("fffffffffff", "2026-10-15T08:00:00.000Z",
              title="https://www.youtube.com/watch?v=fffffffffff", chan=None),  # removed video
        {"header": "YouTube", "title": "Viewed Some community post",
         "titleUrl": "https://www.youtube.com/post/Ugkx", "time": "2026-10-15T07:00:00.000Z"},
        watch("ggggggggggg", "2026-10-15T06:00:00.000Z",
              details=[{"name": "From Google Ads"}]),                          # ad
        watch("hhhhhhhhhhh", "2026-10-15T05:00:00.000Z"),
        watch("hhhhhhhhhhh", "2026-10-15T05:00:00.000Z"),                       # exact duplicate
        watch("iiiiiiiiiii", "not-a-time"),                                     # bad time
        {"header": "YouTube", "title": "Watched no url", "time": "2026-10-15T04:00:00.000Z"},
        watch("jjjjjjjjjjj", "2026-10-15T03:00:00Z",
              title="CafÃ© â\u0080\u0094 mojibake", chan=("  Spaced \n Out  ", UC2)),
        watch("kkkkkkkkkkk", "2026-10-15T02:00:00.000Z", title="日本語 \U0001F3B5",
              chan=("チャンネル \U0001F3B5", UC2)),
        watch("mmmmmmmmmmm", "2026-10-15T00:30:00.000Z", title="A Song",
              chan=("Artist - Topic", UC2), header="YouTube Music"),
        {"header": "YouTube", "title": "Used Shorts", "time": "2026-10-15T00:20:00Z",
         "products": ["YouTube"], "activityControls": ["YouTube watch history"]},
        {"header": "YouTube", "title": "Watched short",
         "titleUrl": "https://www.youtube.com/shorts/lllllllllll", "time": "2026-10-15T01:00:00.000Z",
         "subtitles": [{"name": "Chan", "url": f"https://www.youtube.com/channel/{UC}"}]},
    ]
