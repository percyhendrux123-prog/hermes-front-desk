"""Durable escalation store (SQLite). One row per escalation key, ever."""
from __future__ import annotations

import json
import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Optional

SCHEMA = """
CREATE TABLE IF NOT EXISTS events (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    key           TEXT NOT NULL UNIQUE,
    category      TEXT NOT NULL,
    level         TEXT NOT NULL,
    override      INTEGER NOT NULL DEFAULT 0,
    title         TEXT NOT NULL,
    detail        TEXT NOT NULL DEFAULT '',
    source        TEXT NOT NULL DEFAULT '',
    ref           TEXT NOT NULL DEFAULT '',
    person        TEXT NOT NULL DEFAULT '',
    status        TEXT NOT NULL,      -- pending | handled | notify | digest | logged | suppressed | delivered | dropped
    decided_by    TEXT NOT NULL DEFAULT '',
    note          TEXT NOT NULL DEFAULT '',
    created_at    REAL NOT NULL,
    woke_at       REAL,
    shown_at      REAL,
    notified_at   REAL,
    channel       TEXT NOT NULL DEFAULT '',
    attempts      INTEGER NOT NULL DEFAULT 0,
    briefed_at    REAL                -- a handled money/approval item shown in a morning brief
);
CREATE TABLE IF NOT EXISTS kv (k TEXT PRIMARY KEY, v TEXT NOT NULL);
"""


class Store:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        with self._conn() as c:
            c.executescript(SCHEMA)
            cols = {r[1] for r in c.execute("PRAGMA table_info(events)")}
            if "briefed_at" not in cols:  # a desk.db made by 0.1.0: its handled items are history
                c.execute("ALTER TABLE events ADD COLUMN briefed_at REAL")
                c.execute("UPDATE events SET briefed_at = created_at WHERE status = 'handled'")

    @contextmanager
    def _conn(self):
        c = sqlite3.connect(str(self.path), timeout=1.0, isolation_level=None)  # short: hooks read this too
        try:
            c.row_factory = sqlite3.Row
            c.execute("PRAGMA journal_mode=WAL")
            yield c
        finally:
            c.close()

    # -- kv -----------------------------------------------------------------
    def get(self, k: str, default: Any = None) -> Any:
        with self._conn() as c:
            r = c.execute("SELECT v FROM kv WHERE k=?", (k,)).fetchone()
        return json.loads(r["v"]) if r else default

    def put(self, k: str, v: Any) -> None:
        with self._conn() as c:
            c.execute("INSERT INTO kv(k,v) VALUES(?,?) ON CONFLICT(k) DO UPDATE SET v=excluded.v", (k, json.dumps(v)))

    # -- events -------------------------------------------------------------
    def add(self, *, key: str, category: str, level: str, override: bool, title: str, detail: str,
            source: str, ref: str, person: str, status: str, now: float) -> Optional[int]:
        """Insert; return the new id, or None when this key was seen before (dedupe)."""
        with self._lock, self._conn() as c:
            cur = c.execute(
                """INSERT OR IGNORE INTO events(key,category,level,override,title,detail,source,ref,person,
                   status,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                (key, category, level, int(override), title[:300], detail[:1000], source, ref, person, status, now),
            )
            return cur.lastrowid if cur.rowcount == 1 else None

    def row(self, event_id: int) -> Optional[sqlite3.Row]:
        with self._conn() as c:
            return c.execute("SELECT * FROM events WHERE id=?", (event_id,)).fetchone()

    def rows(self, where: str = "1=1", params: tuple = ()) -> list[sqlite3.Row]:
        with self._conn() as c:
            return list(c.execute(f"SELECT * FROM events WHERE {where} ORDER BY id", params))

    def transition(self, event_id: int, from_status: tuple, **fields: Any) -> bool:
        """Compare-and-set on status: True only for the one caller that moved it."""
        sets = ",".join(f"{k}=?" for k in fields)
        marks = ",".join("?" for _ in from_status)
        with self._lock, self._conn() as c:
            cur = c.execute(
                f"UPDATE events SET {sets} WHERE id=? AND status IN ({marks})",
                (*fields.values(), event_id, *from_status),
            )
            return cur.rowcount == 1

    def claim(self, event_id: int, column: str, now: float) -> bool:
        """Set a once-only timestamp column (woke_at). True only for the first caller."""
        assert column in ("woke_at",)
        with self._lock, self._conn() as c:
            cur = c.execute(f"UPDATE events SET {column}=? WHERE id=? AND {column} IS NULL", (now, event_id))
            return cur.rowcount == 1

    def touch(self, ids: list[int], column: str, now: float) -> None:
        assert column in ("shown_at", "briefed_at")
        if not ids:
            return
        with self._lock, self._conn() as c:
            c.executemany(f"UPDATE events SET {column}=? WHERE id=?", [(now, i) for i in ids])
