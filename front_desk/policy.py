"""Pure decisions: which category, which level, which channel. No I/O."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, time as dtime
from typing import Optional

from .settings import CATEGORIES, FLOORS

_RANK = {"never": 0, "log": 1, "digest": 2, "interrupt": 3}

# Kanban-derived base categories that keyword rules may refine. A finished card stays a
# finished card: Kanban already tells the card's creator, and a "deploy" word in a done
# card's summary is not a live change waiting on anyone.
_REFINABLE = {"card_needs_input", "card_failed", "other"}
_REFINE_ORDER = ("money", "approval", "client_escalation", "deploy")


@dataclass
class Event:
    key: str                # dedupe key: one escalation, one row, ever
    category: str
    title: str
    detail: str = ""
    source: str = ""        # kanban | cron | inbound | tool | cli
    ref: str = ""           # card id, job id, ...
    person: str = ""        # a named person this is from/about, if matched
    level: str = ""         # a watch's own level; empty = the category's level


def match_person(text: str, settings: dict) -> str:
    low = (text or "").lower()
    for p in settings.get("people", []):
        for m in p["match"]:
            if m and m in low:
                return p["name"]
    return ""


def classify(base: str, text: str, settings: dict) -> str:
    """Refine a base category from the settings' keyword lists."""
    if base not in CATEGORIES:
        base = "other"
    if base not in _REFINABLE:
        return base
    low = (text or "").lower()
    for cat in _REFINE_ORDER:
        for kw in settings.get("keywords", {}).get(cat, []) or []:
            if kw and kw.lower() in low:
                return cat
    return base


def is_override(ev: Event, settings: dict) -> bool:
    ai = settings.get("always_interrupt", {})
    if ev.category in (ai.get("categories") or []):
        return True
    names = {n.lower() for n in (ai.get("people") or [])}
    return bool(ev.person and ev.person.lower() in names)


def rule_for(ev: Event, settings: dict) -> Optional[tuple[int, dict]]:
    """The newest saved correction rule that matches, as (number, rule). Numbers start at 1."""
    found = None
    for i, r in enumerate(settings.get("rules") or [], start=1):
        if r.get("category") != ev.category:
            continue
        if r.get("person") and r["person"].lower() != (ev.person or "").lower():
            continue
        if r.get("watch") and not ev.key.startswith(f"watch:{r['watch']}:"):
            continue
        found = (i, r)
    return found


def floor(category: str, level: str) -> str:
    """Locked floors: money and approval sends never drop below the digest."""
    if category in FLOORS and _RANK.get(level, 0) < _RANK["digest"]:
        return "digest"
    return level


def level_for(ev: Event, settings: dict) -> str:
    if is_override(ev, settings):
        return "interrupt"
    hit = rule_for(ev, settings)
    if hit:
        return floor(ev.category, hit[1]["level"])
    if ev.level:
        return floor(ev.category, ev.level)
    return floor(ev.category, settings["levels"].get(ev.category, settings["levels"]["other"]))


def _hhmm(s: str) -> dtime:
    h, m = s.split(":")
    return dtime(int(h), int(m))


def in_quiet_hours(now: datetime, settings: dict) -> bool:
    qh = settings.get("quiet_hours")
    if not qh:
        return False
    start, end, t = _hhmm(qh["start"]), _hhmm(qh["end"]), now.time()
    if start == end:
        return False
    if start < end:
        return start <= t < end
    return t >= start or t < end  # wraps midnight


def phone_target(settings: dict) -> Optional[dict]:
    ph = settings.get("phone") or {}
    if ph.get("session_key"):
        return {"session_key": ph["session_key"]}
    if isinstance(ph.get("origin"), dict) and ph["origin"].get("platform") and ph["origin"].get("chat_id"):
        return {"origin": dict(ph["origin"])}
    return None


def route(urgency: str, presence: str, override: bool, now: datetime, settings: dict) -> tuple[str, str]:
    """(urgency, channel) after quiet hours and presence.

    Quiet hours turn a non-override interrupt into a digest item. An override ignores
    quiet hours. A phone channel with no phone chat configured becomes "none": we never
    guess a chat, because a guessed chat could be someone else's.
    """
    if presence not in ("present", "away", "unknown"):
        presence = "unknown"
    # Quiet hours protect you from being woken; they never hide news while you're at the desk.
    if urgency == "interrupt" and not override and presence != "present" and in_quiet_hours(now, settings):
        urgency = "digest"
    channel = settings["channels"][urgency][presence]
    if channel == "phone" and phone_target(settings) is None:
        channel = "none"
    return urgency, channel


def slot_due(slot: Optional[str], last: Optional[datetime], now: datetime) -> bool:
    """One daily slot ("07:30"): True once today's slot has passed and ``last`` is before it."""
    if not slot:
        return False
    at = datetime.combine(now.date(), _hhmm(slot), tzinfo=now.tzinfo)
    return now >= at and (last is None or last < at)


def digest_due(last: Optional[datetime], now: datetime, settings: dict) -> bool:
    """True when a digest slot has passed since the last digest (or since `last` is None: today's
    first slot already passed)."""
    times = sorted(_hhmm(t) for t in settings.get("digest_times") or [])
    if not times:
        return False
    slots = [datetime.combine(now.date(), t, tzinfo=now.tzinfo) for t in times]
    passed = [s for s in slots if s <= now]
    if not passed:
        return False
    latest = passed[-1]
    return last is None or last < latest
