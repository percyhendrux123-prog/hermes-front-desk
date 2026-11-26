"""Shared test scaffolding: a desk with a fake clock, a recording deliverer and temp files."""
from __future__ import annotations

import os
import sqlite3
import sys
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from front_desk import settings as settings_mod  # noqa: E402

try:
    import yaml  # noqa: F401
    HAVE_YAML = True
except ImportError:  # bare Python: YAML-only tests skip with this message, the rest run
    HAVE_YAML = False
NO_YAML = "PyYAML not installed; run: uv run --no-project --python 3.12 --with pyyaml --with ruamel.yaml==0.18.16 python -m unittest discover -s tests"
from front_desk.desk import Desk  # noqa: E402
from front_desk.policy import Event  # noqa: E402
from front_desk.store import Store  # noqa: E402

TZ = "America/Chicago"
PHONE_KEY = "agent:main:telegram:dm:100000001"
DESK_KEY = "agent:main:local:desk-session-1"


def at(hh: int, mm: int = 0, day: int = 4) -> float:
    return datetime(2026, 3, day, hh, mm, tzinfo=ZoneInfo(TZ)).timestamp()


class Clock:
    def __init__(self, t: float):
        self.t = t

    def __call__(self) -> float:
        return self.t

    def advance(self, seconds: float) -> None:
        self.t += seconds


class Recorder:
    def __init__(self, accept: bool = True):
        self.sent: list[tuple[str, dict]] = []
        self.accept = accept

    def __call__(self, text: str, target: dict) -> bool:
        self.sent.append((text, dict(target)))
        return self.accept

    def to(self, key: str) -> list[str]:
        return [t for t, tg in self.sent if tg.get("session_key") == key]


def base_settings(**over) -> dict:
    raw = {"timezone": TZ, "phone": {"session_key": PHONE_KEY}, "quiet_hours": {"start": "22:00", "end": "07:00"},
           "digest_times": ["09:00", "17:00"]}
    raw.update(over)
    return settings_mod.normalize(raw)


class DeskCase(unittest.TestCase):
    full_presence = False

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="fd-test-", dir=os.environ.get("TMPDIR") or tempfile.gettempdir()))
        self.clock = Clock(at(14))
        self.sent = Recorder()
        self.cfg = base_settings()
        self.state_db = self.tmp / "state.db"
        self.desk = Desk(store=Store(self.tmp / "desk.db"), settings=lambda: self.cfg, deliver=self.sent,
                         state_db=self.state_db, full_presence=self.full_presence, clock=self.clock)

    # presence helpers
    def make_state_db(self, user_input_at: float | None, source: str = "desktop", content: str = "hi"):
        con = sqlite3.connect(self.state_db)
        con.execute("CREATE TABLE IF NOT EXISTS sessions (id TEXT PRIMARY KEY, source TEXT NOT NULL)")
        con.execute("CREATE TABLE IF NOT EXISTS messages (id INTEGER PRIMARY KEY, session_id TEXT, role TEXT,"
                    " content TEXT, timestamp REAL)")
        con.execute("INSERT OR IGNORE INTO sessions VALUES (?, ?)", (f"s-{source}", source))
        if user_input_at is not None:
            con.execute("INSERT INTO messages(session_id, role, content, timestamp) VALUES (?,?,?,?)",
                        (f"s-{source}", "user", content, user_input_at))
        con.commit()
        con.close()

    def at_desk(self, minutes_ago: float = 1):
        """User typed in the desktop session `minutes_ago` minutes ago."""
        self.desk.note_turn(session_key=DESK_KEY, surface="desktop", user_message="hello")
        self.make_state_db(self.clock() - minutes_ago * 60)

    def ev(self, key="k1", category="card_needs_input", title="Card t_1 is waiting on a decision", person=""):
        return Event(key=key, category=category, title=title, source="test", ref="t_1", person=person)
