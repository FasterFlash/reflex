"""Feedback box: small messages stored in a local SQLite file.

Stores only: when, the kind, an optional 1-5 star rating, the message, and an optional reply
address the sender typed themselves. No IP address, no user agent, nothing from any report.
"""
from __future__ import annotations

import re
import sqlite3
import threading
import time

KINDS = ("broke", "confused", "idea", "hello")
MAX_MESSAGE = 2000
MIN_MESSAGE = 5
MAX_CONTACT = 120

_CTRL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


def clean(body: object) -> dict | None:
    """Return a validated record, or None if the request isn't usable.

    Two shapes are accepted. A normal note needs a kind and a message. A star rating (1 to 5) stands
    on its own: the kind and the message are then optional."""
    if not isinstance(body, dict):
        return None
    kinds = body.get("kinds", body.get("kind"))
    if kinds is None:
        kinds = []
    if isinstance(kinds, str):
        kinds = [kinds]
    msg = body.get("message")
    msg = "" if msg is None else msg
    contact = body.get("contact") or ""
    rating = body.get("rating")
    if rating is not None and (isinstance(rating, bool) or not isinstance(rating, int) or not 1 <= rating <= 5):
        return None
    if not isinstance(kinds, list) or not all(k in KINDS for k in kinds) or not isinstance(msg, str) or not isinstance(contact, str):
        return None
    kind = ",".join(k for k in KINDS if k in kinds)        # canonical order, no duplicates
    msg = _CTRL.sub("", msg).strip()
    contact = _CTRL.sub("", contact).strip()[:MAX_CONTACT]
    if len(msg) > MAX_MESSAGE:
        return None
    if rating is None and (not kind or len(msg) < MIN_MESSAGE):
        return None
    return {"kind": kind, "message": msg, "contact": contact, "rating": rating}


class FeedbackStore:
    def __init__(self, path: str):
        self.path = path
        self._lock = threading.Lock()
        with self._conn() as c:
            c.execute("CREATE TABLE IF NOT EXISTS feedback (id INTEGER PRIMARY KEY, ts INTEGER NOT NULL, "
                      "kind TEXT NOT NULL, message TEXT NOT NULL, contact TEXT NOT NULL DEFAULT '')")
            if "rating" not in [r[1] for r in c.execute("PRAGMA table_info(feedback)")]:
                c.execute("ALTER TABLE feedback ADD COLUMN rating INTEGER")

    def _conn(self) -> sqlite3.Connection:
        return sqlite3.connect(self.path, timeout=5)

    def add(self, rec: dict) -> None:
        with self._lock, self._conn() as c:
            c.execute("INSERT INTO feedback (ts, kind, message, contact, rating) VALUES (?,?,?,?,?)",
                      (int(time.time()), rec["kind"], rec["message"], rec["contact"], rec.get("rating")))

    def recent(self, n: int = 50) -> list[dict]:
        with self._conn() as c:
            rows = c.execute("SELECT id, ts, kind, message, contact, rating FROM feedback ORDER BY id DESC LIMIT ?", (n,)).fetchall()
        return [dict(zip(("id", "ts", "kind", "message", "contact", "rating"), r)) for r in rows]
