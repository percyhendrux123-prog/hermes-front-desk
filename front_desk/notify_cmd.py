"""/notify in plain words: "stop telling me about finished cards".

A small fixed grammar, no model call: the same words always make the same change, and every
change is echoed back with how to undo it. Words it doesn't understand change nothing.
"""
from __future__ import annotations

import copy
import re
from typing import Optional

from .settings import CATEGORY_LABELS, LEVEL_LABELS, normalize

# Longest phrases first so "failed cards" beats "cards".
CATEGORY_WORDS = [
    ("client_escalation", ["client escalations", "client escalation", "clients", "client", "customers"]),
    ("money", ["money", "payments", "payment", "billing", "invoices", "refunds", "stripe"]),
    ("deploy", ["deploys", "deploy", "deployments", "live changes", "live change", "releases"]),
    ("card_needs_input", ["blocked cards", "cards waiting on me", "questions", "decisions", "needs input",
                          "blocked", "waiting cards"]),
    ("card_failed", ["failed cards", "stuck cards", "crashed cards", "failures", "failed", "stuck", "crashes"]),
    ("card_done", ["finished cards", "done cards", "completed cards", "finished", "completed", "done"]),
    ("cron_error", ["cron errors", "cron", "scheduled jobs", "scheduled job", "jobs"]),
    ("inbound_person", ["messages from people", "people messages", "inbound"]),
    ("other", ["everything else", "anything else", "other"]),
]

LEVEL_PATTERNS = [
    ("never", r"\b(never|ignore|drop)\b"),
    ("log", r"\b(stop telling me|don'?t tell me|do not tell me|stop notifying me|just log|log only|only log|quiet about|mute)\b"),
    ("digest", r"\b(digest|summary|batch|later|end of day)\b"),
    ("interrupt", r"\b(interrupt|right away|immediately|ping me|tell me now|alert me|wake me|urgent)\b"),
]


class Unparsed(ValueError):
    pass


def _category(text: str) -> Optional[str]:
    low = text.lower()
    best = None
    for cat, words in CATEGORY_WORDS:
        for w in words:
            i = low.find(w)
            if i >= 0 and (best is None or len(w) > best[1]):
                best = (cat, len(w))
    return best[0] if best else None


def apply(text: str, settings: dict) -> tuple[dict, str]:
    """Return (new settings, plain-English confirmation). Raises Unparsed when unsure."""
    s = copy.deepcopy(settings)
    raw = " ".join((text or "").split())
    low = raw.lower()
    if not low:
        raise Unparsed("empty")

    m = re.search(r"quiet hours?\s+(off|none)", low)
    if m:
        old = s.get("quiet_hours")
        s["quiet_hours"] = None
        normalize(s)
        undo = f"/notify quiet hours {old['start']} to {old['end']}" if old else "nothing to undo"
        return s, f"Quiet hours are off. Undo: {undo}"

    m = re.search(r"quiet hours?\s+(?:from\s+)?(\S+)\s+(?:to|until|-)\s+(\S+)", low)
    if m:
        start, end = _clock(m.group(1)), _clock(m.group(2))
        old = s.get("quiet_hours")
        s["quiet_hours"] = {"start": start, "end": end}
        normalize(s)
        undo = f"/notify quiet hours {old['start']} to {old['end']}" if old else "/notify quiet hours off"
        return s, f"Quiet hours are now {start} to {end}. Undo: {undo}"

    m = re.search(r"digests? (?:at|times?)\s+(.+)", low)
    if m:
        times = [_clock(t) for t in re.split(r"\s*(?:,|and)\s*", m.group(1)) if t.strip()]
        old = list(s["digest_times"])
        s["digest_times"] = times
        normalize(s)
        return s, f"Digest times are now {', '.join(times)}. Undo: /notify digest at {', '.join(old)}"

    m = re.search(r"always (?:interrupt|tell me|ping me)(?: me)? (?:for|about|when)\s+(.+)", raw, re.I)
    if m:
        target = m.group(1).strip().rstrip(".")
        cat = _category(target)
        ai = s["always_interrupt"]
        if cat and cat != "other":
            if cat not in ai["categories"]:
                ai["categories"].append(cat)
            normalize(s)
            return s, (f"{CATEGORY_LABELS[cat].capitalize()} will always interrupt, even in quiet hours. "
                       f"Undo: /notify stop always interrupting for {CATEGORY_LABELS[cat]}")
        name = re.sub(r"^(messages from|anything from|anyone named)\s+", "", target, flags=re.I).strip()
        if name not in ai["people"]:
            ai["people"].append(name)
        if not any(p["name"].lower() == name.lower() for p in s["people"]):
            s["people"].append({"name": name, "match": [name.lower()]})
        normalize(s)
        return s, (f"Anything from or about {name} will always interrupt, even in quiet hours. "
                   f"Undo: /notify stop always interrupting for {name}")

    m = re.search(r"stop always (?:interrupting|telling me|pinging me)(?: me)? (?:for|about)\s+(.+)", raw, re.I)
    if m:
        target = m.group(1).strip().rstrip(".")
        cat = _category(target)
        ai = s["always_interrupt"]
        if cat and cat in ai["categories"]:
            ai["categories"].remove(cat)
            normalize(s)
            return s, f"{CATEGORY_LABELS[cat].capitalize()} no longer always interrupt; they follow their level."
        for n in list(ai["people"]):
            if n.lower() == target.lower():
                ai["people"].remove(n)
                normalize(s)
                return s, f"{n} no longer always interrupts."
        raise Unparsed(f"nothing set to always interrupt for {target!r}")

    level = None
    for lvl, pat in LEVEL_PATTERNS:
        if re.search(pat, low):
            level = lvl
            break
    cat = _category(low)
    if level and cat:
        old = s["levels"][cat]
        s["levels"][cat] = level
        normalize(s)
        return s, (f"{CATEGORY_LABELS[cat].capitalize()}: {LEVEL_LABELS[level]} (was: {LEVEL_LABELS[old]}). "
                   f"Undo: /notify {_undo_phrase(old)} {CATEGORY_LABELS[cat]}")
    raise Unparsed("no category or level recognised")


def _undo_phrase(level: str) -> str:
    return {"interrupt": "interrupt me for", "digest": "put in the digest:", "log": "stop telling me about",
            "never": "never show"}[level]


def _clock(t: str) -> str:
    t = t.strip().lower().replace(".", "")
    m = re.fullmatch(r"(\d{1,2})(?::(\d{2}))?\s*(am|pm)?", t)
    if not m:
        raise Unparsed(f"can't read the time {t!r}")
    h, mi, ap = int(m.group(1)), int(m.group(2) or 0), m.group(3)
    if ap == "pm" and h != 12:
        h += 12
    if ap == "am" and h == 12:
        h = 0
    if not (0 <= h <= 23 and 0 <= mi <= 59):
        raise Unparsed(f"can't read the time {t!r}")
    return f"{h:02d}:{mi:02d}"


HELP = """I didn't change anything. Try one of these:
/notify                                   show the current settings
/notify stop telling me about finished cards
/notify interrupt me for failed cards
/notify put cron errors in the digest
/notify never show scheduled jobs
/notify always interrupt for Dana Reyes
/notify quiet hours 10pm to 7am   (or: quiet hours off)
/notify digest at 9am and 5pm"""
