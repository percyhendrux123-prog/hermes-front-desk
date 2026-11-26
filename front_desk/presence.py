"""Is the user at the desk? Feature-detected, never version-sniffed.

Full mode (Hermes with ``plugin_session.session_viewers``): read the clients attached to the
orchestrator's session and their idle time. Viewers only answer inside that session's own
context (a turn or a tool call), so the desk snapshots them there and ages the snapshot when it
must decide from a timer.

Degraded mode (no ``session_viewers``): the user's newest typed input in a local session
(desktop/TUI/CLI) in ``state.db``, read-only. Text the desk itself injected is not the user.
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Optional

LOCAL_SOURCES = ("local", "cli", "tui", "desktop", "gui", "acp")
MARKER = "[front-desk]"


@dataclass
class Presence:
    state: str              # present | away | unknown
    mode: str               # full | degraded
    basis: str = ""         # what it was read from
    idle_seconds: Optional[float] = None


def detect_viewers_fn(plugin_session_module: Any, ctx: Any = None) -> Optional[Callable[..., Any]]:
    """Return a callable giving the session's viewers, or None when this Hermes has none."""
    fn = getattr(ctx, "session_viewers", None) if ctx is not None else None
    if callable(fn):
        return fn
    fn = getattr(plugin_session_module, "session_viewers", None) if plugin_session_module else None
    return fn if callable(fn) else None


def from_viewers(viewers: list, idle_limit_s: float, age_s: float = 0.0) -> Presence:
    """Full-mode reading. ``age_s`` = seconds since the snapshot was taken."""
    if not viewers:
        return Presence("away", "full", "no client attached")
    best: Optional[float] = None
    for v in viewers:
        if not isinstance(v, dict):
            continue
        if v.get("away") is True:  # screen locked, when the host passes it through
            continue
        idle = v.get("idle_seconds")
        if idle is None:
            continue
        idle = float(idle) + max(0.0, age_s)
        best = idle if best is None else min(best, idle)
    if best is None:
        if any(isinstance(v, dict) and v.get("away") is True for v in viewers):
            return Presence("away", "full", "screen locked")
        return Presence("unknown", "full", "attached, no input seen")
    state = "present" if best <= idle_limit_s else "away"
    return Presence(state, "full", "client activity", best)


def from_state_db(db_path: Path, now: float, idle_limit_s: float) -> Presence:
    """Degraded reading from state.db: newest user input in a local session."""
    if not db_path.exists():
        return Presence("unknown", "degraded", "no state.db")
    try:
        uri = f"file:{db_path}?mode=ro"
        con = sqlite3.connect(uri, uri=True, timeout=1.0)
        try:
            marks = ",".join("?" for _ in LOCAL_SOURCES)
            row = con.execute(
                f"""SELECT MAX(m.timestamp) FROM messages m JOIN sessions s ON s.id = m.session_id
                    WHERE s.source IN ({marks}) AND m.role = 'user'
                      AND COALESCE(m.content, '') NOT LIKE ?""",
                (*LOCAL_SOURCES, MARKER + "%"),
            ).fetchone()
        finally:
            con.close()
    except sqlite3.Error:
        return Presence("unknown", "degraded", "state.db unreadable")
    ts = row[0] if row else None
    if ts is None:
        return Presence("unknown", "degraded", "no local input yet")
    idle = max(0.0, now - float(ts))
    return Presence("present" if idle <= idle_limit_s else "away", "degraded", "last local input", idle)
