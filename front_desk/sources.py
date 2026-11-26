"""Read-only intake from the shared Kanban board and the cron job list.

Both are polled from the orchestrator's own process, so an escalation from ANY lane (any
profile's worker, a responder that opened a card, a scheduled job) reaches the desk even when
that lane never loads this plugin. Read-only: the desk never writes to either file.
"""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any, Optional

from .policy import Event, classify, match_person

KANBAN_KINDS = ("blocked", "completed", "review_requested", "crashed", "gave_up", "timed_out",
                "block_loop_detected")
CRON_OK = {None, "ok", "cancelled", "skipped", "silent"}
DESK_AUTHOR = "front-desk"   # cards the desk itself made (watch cards) are never its own intake
WAITING_ON_BOSS = (None, "", "needs_input", "capability")


def _has_col(con, table: str, col: str) -> bool:
    try:
        return any(r[1] == col for r in con.execute(f"PRAGMA table_info({table})"))
    except sqlite3.Error:
        return False


def _short(text: Any, n: int = 280) -> str:
    s = " ".join(str(text or "").split())
    return s if len(s) <= n else s[: n - 1] + "…"


def poll_kanban(db_path: Path, cursor: Optional[int], settings: dict) -> tuple[list[Event], Optional[int]]:
    """New board events after ``cursor`` (a task_events id). First call (cursor None) only takes
    the current high-water mark, so installing the desk never replays the board's history."""
    if not Path(db_path).exists():
        return [], cursor
    con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=2.0)
    try:
        if cursor is None:
            top = con.execute("SELECT COALESCE(MAX(id), 0) FROM task_events").fetchone()[0]
            return [], int(top)
        marks = ",".join("?" for _ in KANBAN_KINDS)
        by = "COALESCE(t.created_by, '')" if _has_col(con, "tasks", "created_by") else "''"
        rows = con.execute(
            f"""SELECT e.id, e.task_id, e.run_id, e.kind, e.payload, t.title, t.assignee
                FROM task_events e LEFT JOIN tasks t ON t.id = e.task_id
                WHERE e.id > ? AND e.kind IN ({marks}) AND {by} != ?
                ORDER BY e.id LIMIT 500""",
            (cursor, *KANBAN_KINDS, DESK_AUTHOR),
        ).fetchall()
        top = con.execute("SELECT COALESCE(MAX(id), ?) FROM task_events", (cursor,)).fetchone()[0]
    finally:
        con.close()
    out: list[Event] = []
    for eid, task_id, _run, kind, payload, title, assignee in rows:
        try:
            p = json.loads(payload or "{}")
        except ValueError:
            p = {}
        ev = _kanban_event(eid, task_id, kind, p, title or task_id, assignee or "", settings)
        if ev is not None:
            out.append(ev)
    return out, int(top)


def _kanban_event(eid, task_id, kind, p, title, assignee, settings) -> Optional[Event]:
    if kind == "blocked":
        bkind = p.get("kind")
        if bkind == "dependency":
            return None  # waiting on another card resolves itself
        base = "card_failed" if bkind == "transient" else "card_needs_input"
        reason = p.get("reason") if p.get("reason") != "initial_status" else ""
        detail = _short(reason)
        verb = "is waiting on a decision" if base == "card_needs_input" else "hit a flaky failure"
    elif kind in ("completed", "review_requested"):
        base, detail = "card_done", _short(p.get("summary"))
        verb = "is done" if kind == "completed" else "is ready for review"
    else:
        if kind == "crashed" and p.get("retry_status") == "ready":
            return None  # the dispatcher retries it; gave_up fires if retries run out
        base, detail = "card_failed", _short(p.get("error") or p.get("worker_output") or "")
        verb = {"crashed": "crashed", "gave_up": "gave up after retries", "timed_out": "timed out",
                "block_loop_detected": "keeps getting re-blocked"}[kind]
    text = f"{title} {detail}"
    return Event(
        key=f"kanban:{eid}",
        category=classify(base, text, settings),
        title=f"Card {task_id} ({assignee or 'unassigned'}) {verb}: {_short(title, 120)}",
        detail=detail, source="kanban", ref=task_id, person=match_person(text, settings),
    )


def poll_cron(jobs_path: Path, seen: Optional[dict], settings: dict) -> tuple[list[Event], dict]:
    """Jobs whose newest run failed. ``seen`` maps job id -> last_run_at already handled. First call
    (seen None) records the current state without alerting."""
    if not Path(jobs_path).exists():
        return [], seen or {}
    try:
        data = json.loads(Path(jobs_path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return [], seen or {}
    jobs = data.get("jobs", []) if isinstance(data, dict) else data
    first = seen is None
    seen = dict(seen or {})
    out: list[Event] = []
    for j in jobs if isinstance(jobs, list) else []:
        if not isinstance(j, dict) or not j.get("id"):
            continue
        jid, run = str(j["id"]), str(j.get("last_run_at") or "")
        if seen.get(jid) == run:
            continue
        seen[jid] = run
        if first or not run or j.get("last_status") in CRON_OK:
            continue
        name = j.get("name") or jid
        streak = j.get("failure_streak") or 1
        detail = _short(j.get("last_error") or j.get("last_delivery_error") or j.get("last_status"), 200)
        out.append(Event(
            key=f"cron:{jid}:{run}", category="cron_error",
            title=f"Scheduled job '{_short(name, 80)}' failed ({j.get('last_status')}, streak {streak})",
            detail=detail, source="cron", ref=jid, person=match_person(f"{name} {detail}", settings),
        ))
    return out, seen


def blocked_on_boss(db_path: Path) -> list[dict]:
    """Cards blocked on a person right now (needs_input / capability / unspecified), with when
    they were blocked. Read-only. Waiting on another card or a flaky failure is not the boss's."""
    if db_path is None or not Path(db_path).exists():
        return []
    con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=2.0)
    try:
        by = "COALESCE(t.created_by, '')" if _has_col(con, "tasks", "created_by") else "''"
        rows = con.execute(
            f"""SELECT t.id, t.title, e.id, e.created_at, e.payload
               FROM tasks t JOIN task_events e ON e.id = (
                   SELECT MAX(id) FROM task_events WHERE task_id = t.id AND kind = 'blocked')
               WHERE t.status = 'blocked' AND {by} != ?
               ORDER BY e.created_at""",
            (DESK_AUTHOR,),
        ).fetchall()
    except sqlite3.Error:
        return []
    finally:
        con.close()
    from .watches import card_code
    out = []
    for task_id, title, eid, created, payload in rows:
        try:
            kind = (json.loads(payload or "{}") or {}).get("kind")
        except ValueError:
            kind = None
        if kind not in WAITING_ON_BOSS:
            continue
        out.append({"task_id": task_id, "code": card_code(task_id, title or ""), "title": title or task_id,
                    "since": float(created), "block_event_id": int(eid)})
    return out
