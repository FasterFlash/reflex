# Reflex backend

Reads a Google Takeout YouTube history and turns it into clean tables plus an honest
data-quality report, then looks up video length and category. Phase 2, steps 1-3 of 8.

## Run it (Windows PowerShell)

```powershell
cd D:\ENIGMA\SEASON7-wandering\reflex\backend
python -m venv .venv
.venv\Scripts\Activate.ps1
pip install -e ".[dev]"
pytest
reflex inspect "C:\path\to\takeout-....zip"
```

Keep real Takeout files out of the repo. Put them in `backend\data\` (git-ignored).

## Look up video length and category

Needs a YouTube Data API key in `backend\.env` (git-ignored):

```
YOUTUBE_API_KEY=your-key-here
```

```powershell
reflex enrich "C:\path\to\takeout.zip" --dry-run   # what would be fetched, no key needed
reflex enrich "C:\path\to\takeout.zip"             # do it; results go to data\cache.sqlite
```

About 170 API calls for 8,500 unique videos. It is resumable and the second run costs
nothing, because the cache remembers every video (including removed ones, retried after
30 days). The cache holds only public facts about videos (length, category, channel),
never who watched what. The key is sent in a header, never in a URL, and never printed.

## What `reflex inspect` tells you

Rows in the file, rows kept, why each dropped row was dropped (posts, ads, duplicates,
bad times, no video id), the date range in IST, active days, and how messy the kept rows
are: removed videos, rows with no channel, events sharing one timestamp.

## Short/Video split and watch time

```powershell
reflex analyze "..\takeouts\takeout-....zip"     # needs the cache from `reflex enrich`
```

- **Short** is 60 s or under. A 61-180 s event is a Short if the video says #shorts, or if
  the event before it and the event after it are both Shorts in the same sitting (a run of
  up to 3 such events between two Shorts is judged together). Anything the context does
  not settle is a **Video**. Over 180 s is a Video. No length (removed, private, live) is
  **Unknown**. Checked on real data: the events this pulls in are swiped through like
  Shorts, the rest are watched like clips. There is no "unsure" bucket.
- **Watch time** = time until your next event, capped at the video's length. Exact when
  another event follows within 30 min. The last event before a longer silence is a guess,
  and on real data that is about a third of the total, so totals are always a range:
  floor <= estimate <= ceiling. The estimate credits those events with the completion rate
  seen in the same person's own sessions for that video length.

## The report

```powershell
reflex report "..\takeouts\takeout-....zip" --out data\report.json
```

One JSON object (`schema_version` 1) with: totals and the Short/Video split, top 5 channels
(by time and by watches), top 5 categories, top 5 searches, subscribed vs watched, a per-day
calendar with streaks, an hour-by-weekday matrix, sessions and the longest Shorts scroll,
rewatches, first/last facts, a rule-based alter ego, and the disclosures the page must show.
Everything is counting and ranking: no AI and no network.

- Searches are grouped by a normalised string (case, punctuation, spacing). Grouping by
  meaning (embeddings) is parked.
- A watch belongs to the IST day, hour and weekday it started.
- A rewatch is the same video again after 10+ minutes.

## Rules worth knowing

- Everything stays in memory. The ZIP is never extracted to disk.
- The ZIP is treated as hostile: size, file-count and compression-ratio caps, and any
  path that tries to escape the archive is refused.
- Files are found by position (`history/watch-history.json`), not by the leading folder
  name, which Google localises. File names inside are still English-only.
- Time is converted to IST once, here. Ties on an identical timestamp are ordered using
  the file order (Takeout is newest-first).
- Channels are keyed by channel ID. A renamed channel stays one channel.
- Mojibake is repaired with `ftfy`. Quotes and character width are left alone.

## Layout

```
src/reflex/errors.py   named errors; `code` matches the upload screen's states
src/reflex/ingest.py   safe ZIP open, finds the three files
src/reflex/parse.py    clean tables + QualityReport
src/reflex/enrich.py   API lookup + SQLite cache (videos.list, videoCategories.list)
src/reflex/config.py   reads the key from the environment or .env
src/reflex/watchtime.py  Short/Video/Unsure + floor/estimate/ceiling watch time
src/reflex/report.py   channels, categories, searches, subscriptions, calendar, rhythm, sessions, rewatches, alter ego
src/reflex/cli.py      `reflex inspect`, `enrich`, `analyze`, `report`
tests/                 fixtures with one row per edge case, 131 tests; the API is mocked
```


## Run the site (upload + report)

```powershell
cd backend
pip install -e ".[dev]"
reflex serve            # http://127.0.0.1:8000, serves the pages from the folder above backend
```

Open http://127.0.0.1:8000/upload.html, drop the ZIP, click **Read my history**. The ZIP is the raw request
body, held in memory, never written to disk. A background job reports progress (reading, lookup, measuring);
the report is handed to the browser once and the job is deleted. The browser keeps it in `sessionStorage`
only, so closing the tab drops it.

- Needs `YOUTUBE_API_KEY` in `backend\.env` for videos not already in `data/cache.sqlite`.
- Only the pages, `jszip.min.js` and `videos/` are served. `backend/`, `takeouts/` and `.env` are not reachable.
- Limits: 2 jobs at once, 15 minute job lifetime, 300 MB upload cap, 8 parallel lookups.
