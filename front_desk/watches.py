"""Responsibilities: standing watches written in plain words.

"card waiting on me over 2 hours" or "nobody waits more than two days for a reply" is a duty the
desk keeps, not an event it receives. Each regular check evaluates every watch and raises its
own escalation when one is breached. A small fixed grammar, no model call: the same words always
mean the same watch, and words it can't read are refused with the reason.

Pure functions only (no I/O), so every rule here is tested on its own.
"""
from __future__ import annotations

import hashlib
import re
from typing import Iterable, Optional

from .policy import Event

NUM_WORDS = {
    "a": 1, "an": 1, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7,
    "eight": 8, "nine": 9, "ten": 10, "eleven": 11, "twelve": 12, "a couple of": 2, "a few": 3,
}
UNIT_S = {"minute": 60, "min": 60, "hour": 3600, "hr": 3600, "h": 3600, "day": 86400, "d": 86400,
          "week": 604800}
_NUM = r"(\d+(?:\.\d+)?|a couple of|a few|an?|one|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve)"
_UNIT = r"(minutes?|mins?|hours?|hrs?|h|days?|d|weeks?)\b"
# A digit may touch its unit ("2h"); a word needs a space ("two days"), so "and", "tend" or "read"
# can never read as a duration.
_DUR = re.compile(r"\b(?:(\d+(?:\.\d+)?)\s*|(a couple of|a few|an?|one|two|three|four|five|six|seven|eight|nine|"
                  r"ten|eleven|twelve)\s+)" + _UNIT)
_CARD = re.compile(r"\bcards?\b")
_CARD_WAIT = re.compile(r"waiting on me|waiting for me|waiting on my|blocked|stuck|waits? on me")
_REPLY = re.compile(r"\brepl(?:y|ies|ied)\b|\banswer(?:ed|s)?\b|\brespon(?:se|ded|d)\b|\bheard? back\b")
_ANYONE = re.compile(r"\b(nobody|no one|noone|anyone|everyone|anybody|any person|people)\b")
_STOP = {"a", "an", "any", "my", "the", "every", "no", "one", "each", "if", "when", "blocked", "stuck", "waiting",
         "urgent", "open", "late", "old", "new", "important", "kanban", "board", "single", "some", "that", "this"}
LEVELS = ("interrupt", "digest")

EXAMPLES = ('"card waiting on me over 2 hours", "tell me if a client card is blocked over 90 minutes", '
            '"nobody waits more than two days for a reply", "Dana Reyes not answered in 1 day"')


class WatchError(ValueError):
    pass


def watch_id(text: str) -> str:
    return hashlib.sha256(" ".join(text.lower().split()).encode()).hexdigest()[:8]


def _duration(low: str) -> int:
    found = list(_DUR.finditer(low))
    if not found:
        raise WatchError("say how long, for example 'over 2 hours' or 'more than two days'")
    if len(found) > 1:
        raise WatchError("give one length of time, for example 'over 150 minutes' instead of '2 hours and 30 minutes'")
    m = found[0]
    digits, word, unit = m.group(1), m.group(2), m.group(3)
    num = float(digits) if digits else float(NUM_WORDS[word])
    key = unit.rstrip("s") if unit not in ("h", "d") else unit
    secs = int(num * UNIT_S[key])
    if secs < 60:
        raise WatchError("the shortest watch is one minute")
    return secs


def parse(entry, people: Iterable[dict]) -> dict:
    """One settings entry (a string, or a mapping with ``text`` plus optional ``level``/``card``)
    -> a normalized watch. Raises WatchError with a reason the user can act on."""
    if isinstance(entry, str):
        entry = {"text": entry}
    if not isinstance(entry, dict) or not str(entry.get("text") or "").strip():
        raise WatchError("each responsibility needs its words under 'text'")
    text = " ".join(str(entry["text"]).split())
    low = text.lower()
    level = str(entry.get("level") or "interrupt")
    if level not in LEVELS:
        raise WatchError(f"{text!r}: level must be interrupt or digest (a watch that never tells you does nothing)")
    out = {"id": watch_id(text), "text": text, "level": level, "card": bool(entry.get("card", False)),
           "kind": "", "seconds": 0, "filter": "", "person": ""}
    try:
        secs = _duration(low)
    except WatchError as exc:
        raise WatchError(f"{text!r}: {exc}") from None
    if _CARD.search(low) and _CARD_WAIT.search(low):
        before = low[: _CARD.search(low).start()].split()
        word = before[-1] if before else ""
        out.update(kind="card_waiting", seconds=secs, filter="" if word in _STOP else word)
        return out
    if _REPLY.search(low):
        if _ANYONE.search(low):
            out.update(kind="reply_wait", seconds=secs, person="")
            return out
        for p in people:
            names = [p["name"].lower(), *(m for m in p.get("match", []) if m)]
            if any(re.search(r"(?<!\w)" + re.escape(n) + r"(?!\w)", low) for n in names):
                out.update(kind="reply_wait", seconds=secs, person=p["name"])
                return out
        raise WatchError(f"{text!r}: name someone from your people list, or say 'nobody'")
    raise WatchError(f"{text!r}: I can watch cards waiting on you, or people waiting on a reply. "
                     f"Examples: {EXAMPLES}")


def describe(w: dict) -> str:
    extra = []
    if w.get("filter"):
        extra.append(f"only cards with '{w['filter']}' in the title")
    if w.get("card"):
        extra.append("shown as a card on the board")
    if w.get("level") == "digest":
        extra.append("goes to the morning brief")
    return w["text"] + (f" ({'; '.join(extra)})" if extra else "")


def ago(seconds: float) -> str:
    s = max(0, int(seconds))
    if s >= 2 * 86400:
        return f"{s // 86400} days"
    if s >= 3600:
        h, m = divmod(s // 60, 60)
        return f"{h}h {m}m" if m else f"{h}h"
    return f"{s // 60} min"


def evaluate(w: dict, now: float, *, blocked_cards: list[dict], inbound: list[dict],
             answered: dict) -> list[Event]:
    """Breaches of one watch, as escalations. Keys are stable per breach, so one breach alerts once.

    ``blocked_cards``: [{task_id, code, title, since, block_event_id}] waiting on the boss.
    ``inbound``: [{id, person, created_at, status}] messages from named people, oldest first.
    ``answered``: {person name (lower): ts}; messages at or before ts don't count (answered by the
    boss, or already reported by this watch, so the next unanswered message starts a new wait).
    """
    out: list[Event] = []
    if w["kind"] == "card_waiting":
        for c in blocked_cards:
            age = now - float(c["since"])
            if age < w["seconds"]:
                continue
            if w["filter"] and w["filter"] not in str(c.get("title", "")).lower():
                continue
            out.append(Event(
                key=f"watch:{w['id']}:card:{c['task_id']}:{c['block_event_id']}", category="responsibility",
                title=f"{c['code']} has waited {ago(age)} on you: {str(c.get('title', ''))[:100]}",
                detail=f"Your watch: {w['text']}", source="watch", ref=str(c["task_id"]), level=w["level"],
            ))
    elif w["kind"] == "reply_wait":
        oldest: dict[str, dict] = {}
        newest: dict[str, float] = {}
        for r in inbound:
            name = str(r.get("person") or "")
            if not name or (w["person"] and name.lower() != w["person"].lower()):
                continue
            if r.get("status") == "handled" or float(answered.get(name.lower(), 0)) >= float(r["created_at"]):
                continue
            oldest.setdefault(name.lower(), r)
            newest[name.lower()] = max(newest.get(name.lower(), 0.0), float(r["created_at"]))
        for low_name, r in oldest.items():
            age = now - float(r["created_at"])
            if age < w["seconds"]:
                continue
            out.append(Event(
                key=f"watch:{w['id']}:reply:{low_name}:{r['id']}", category="responsibility",
                title=f"{r['person']} has waited {ago(age)} for a reply", detail=f"Your watch: {w['text']}",
                source="watch", ref=str(r.get("ref") or ""), person=str(r["person"]), level=w["level"],
                since=newest[low_name],  # this wait covers every message so far
            ))
    return out


_CODE = re.compile(r"^\s*([A-Z]{1,5}-?\d{1,6})\b")


def card_code(task_id: str, title: str) -> str:
    """The short code a person uses for a card: a leading 'A101'-style tag, else the card id."""
    m = _CODE.match(title or "")
    return m.group(1) if m else str(task_id)
