"""/notify in plain words: "stop telling me about finished cards".

A small fixed grammar, no model call: the same words always make the same change, and every
change is echoed back with how to undo it. Words it doesn't understand change nothing.
"""
from __future__ import annotations

import copy
import re
from typing import Optional

from .settings import CATEGORY_LABELS, FLOORS, LEVEL_LABELS, describe_rule, normalize

# Longest phrases first so "failed cards" beats "cards".
CATEGORY_WORDS = [
    ("client_escalation", ["client escalations", "client escalation", "clients", "client", "customers"]),
    ("money", ["money", "payments", "payment", "billing", "invoices", "refunds", "stripe"]),
    ("approval", ["sends waiting on my approval", "sends waiting on approval", "approval sends", "approvals",
                  "approval"]),
    ("deploy", ["deploys", "deploy", "deployments", "live changes", "live change", "releases"]),
    ("card_needs_input", ["blocked cards", "cards waiting on me", "questions", "decisions", "needs input",
                          "blocked", "waiting cards"]),
    ("card_failed", ["failed cards", "stuck cards", "crashed cards", "failures", "failed", "stuck", "crashes"]),
    ("card_done", ["finished cards", "done cards", "completed cards", "finished", "completed", "done"]),
    ("cron_error", ["cron errors", "cron", "scheduled jobs", "scheduled job", "jobs"]),
    ("inbound_person", ["messages from people", "people messages", "inbound"]),
    ("responsibility", ["standing watches", "my watches", "watches", "responsibilities"]),
    ("other", ["everything else", "anything else", "other"]),
]

LEVEL_PATTERNS = [
    ("never", r"\b(never|ignore|drop)\b"),
    ("log", r"\b(stop telling me|don'?t tell me|do not tell me|stop notifying me|just log|log only|only log|quiet about|mute)\b"),
    ("digest", r"\b(digest|summary|batch|later|end of day|brief|morning)\b"),
    ("interrupt", r"\b(interrupt|right away|immediately|ping me|tell me now|alert me|wake me|urgent)\b"),
]


class Unparsed(ValueError):
    pass


class SettingsError(ValueError):
    """A change was understood but refused (for example a locked floor). Message says why."""


def _normalize_or_explain(s: dict) -> dict:
    from .settings import SettingsError as _SE
    try:
        return normalize(s)
    except _SE as exc:
        raise SettingsError(str(exc)) from None


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

    m = re.match(r"^(?:watch|add (?:a )?(?:watch|responsibility)|responsibility)[:\s]+(.+)$", raw, re.I)
    if m:
        from . import watches
        text = m.group(1).strip().rstrip(".")
        try:
            w = watches.parse(text, s["people"])
        except watches.WatchError as exc:
            raise SettingsError(f"I couldn't read that watch: {exc}") from None
        if any(x["id"] == w["id"] for x in s["responsibilities"]):
            return s, f"Already watching: {w['text']}. Nothing changed."
        s["responsibilities"].append(w)
        s = _normalize_or_explain(s)
        n = len(s["responsibilities"])
        return s, (f"Watching: {watches.describe(w)}. I check it every {s['check_every_hours']:g} hours "
                   f"and tell you only when it's breached. Undo: /notify stop watching {n}")

    m = re.match(r"^stop watching\s+(\d+)$", low)
    if m:
        i = int(m.group(1))
        if not 1 <= i <= len(s["responsibilities"]):
            raise SettingsError(f"there's no watch number {i}; /notify shows the list")
        w = s["responsibilities"].pop(i - 1)
        _normalize_or_explain(s)
        return s, f"Stopped watching: {w['text']}. Undo: /notify watch {w['text']}"

    m = re.match(r"^(?:morning )?brief (?:at\s+)?(off|\S+)$", low)
    if m:
        old = s.get("brief")
        undo = f"/notify brief at {old['time']}" if old else "/notify brief off"
        if m.group(1) == "off":
            s["brief"] = None
            _normalize_or_explain(s)
            return s, f"The morning brief is off. Undo: {undo}"
        t = _clock(m.group(1))
        s["brief"] = {"time": t}
        _normalize_or_explain(s)
        return s, f"The morning brief comes at {t}. Undo: {undo}"

    m = re.match(r"^check every\s+(\d+(?:\.\d+)?)\s*(?:hours?|h)$|^checks? off$", low)
    if m:
        old = s.get("check_every_hours") or 0
        s["check_every_hours"] = float(m.group(1)) if m.group(1) else 0
        _normalize_or_explain(s)
        undo = f"/notify check every {old:g} hours" if old else "/notify checks off"
        now = (f"every {s['check_every_hours']:g} hours" if s["check_every_hours"] else "off")
        return s, f"Checks are {now}. Undo: {undo}"

    m = re.search(r"quiet hours?\s+(off|none)", low)
    if m:
        old = s.get("quiet_hours")
        s["quiet_hours"] = None
        _normalize_or_explain(s)
        undo = f"/notify quiet hours {old['start']} to {old['end']}" if old else "nothing to undo"
        return s, f"Quiet hours are off. Undo: {undo}"

    m = re.search(r"quiet hours?\s+(?:from\s+)?(\S+)\s+(?:to|until|-)\s+(\S+)", low)
    if m:
        start, end = _clock(m.group(1)), _clock(m.group(2))
        old = s.get("quiet_hours")
        s["quiet_hours"] = {"start": start, "end": end}
        _normalize_or_explain(s)
        undo = f"/notify quiet hours {old['start']} to {old['end']}" if old else "/notify quiet hours off"
        return s, f"Quiet hours are now {start} to {end}. Undo: {undo}"

    m = re.search(r"digests? (?:at|times?)\s+(.+)", low)
    if m:
        times = [_clock(t) for t in re.split(r"\s*(?:,|and)\s*", m.group(1)) if t.strip()]
        old = list(s["digest_times"])
        s["digest_times"] = times
        _normalize_or_explain(s)
        return s, f"Digest times are now {', '.join(times)}. Undo: /notify digest at {', '.join(old)}"

    m = re.search(r"always (?:interrupt|tell me|ping me)(?: me)? (?:for|about|when)\s+(.+)", raw, re.I)
    if m:
        target = m.group(1).strip().rstrip(".")
        cat = _category(target)
        ai = s["always_interrupt"]
        if cat and cat != "other":
            if cat not in ai["categories"]:
                ai["categories"].append(cat)
            _normalize_or_explain(s)
            return s, (f"{CATEGORY_LABELS[cat].capitalize()} will always interrupt, even in quiet hours. "
                       f"Undo: /notify stop always interrupting for {CATEGORY_LABELS[cat]}")
        name = re.sub(r"^(messages from|anything from|anyone named)\s+", "", target, flags=re.I).strip()
        if name not in ai["people"]:
            ai["people"].append(name)
        if not any(p["name"].lower() == name.lower() for p in s["people"]):
            s["people"].append({"name": name, "match": [name.lower()]})
        _normalize_or_explain(s)
        return s, (f"Anything from or about {name} will always interrupt, even in quiet hours. "
                   f"Undo: /notify stop always interrupting for {name}")

    m = re.search(r"stop always (?:interrupting|telling me|pinging me)(?: me)? (?:for|about)\s+(.+)", raw, re.I)
    if m:
        target = m.group(1).strip().rstrip(".")
        cat = _category(target)
        ai = s["always_interrupt"]
        if cat and cat in ai["categories"]:
            ai["categories"].remove(cat)
            _normalize_or_explain(s)
            return s, f"{CATEGORY_LABELS[cat].capitalize()} no longer always interrupt; they follow their level."
        for n in list(ai["people"]):
            if n.lower() == target.lower():
                ai["people"].remove(n)
                _normalize_or_explain(s)
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
        _normalize_or_explain(s)
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
/notify digest at 9am and 5pm
/notify brief at 7:30am   (or: brief off)
/notify check every 3 hours   (or: checks off)
/notify watch card waiting on me over 2 hours
/notify watch nobody waits more than two days for a reply
/notify stop watching 1
/notify that could have waited, vendor updates only matter when a date changes
/notify yes                (saves the rule it just offered)
/notify forget rule 1
/notify answered Dana Reyes"""


# -- corrections: "that could have waited, <why>" -------------------------------------------
_CORRECTION = re.compile(
    r"^(?:that|this|it|the last one)\s+(?:could|can|should|might)\s*(?:'?ve|\s+have)?\s+waited\b"
    r"|^(?:that|this|it)\s+(?:should|could)\s+(?:have\s+)?(?:been|gone)\s+in\s+the\s+(?:morning\s+)?(?:brief|digest)\b"
    r"|^(?:that|this|it)\s+(?:wasn'?t|was not)\s+urgent\b", re.I)
_LEAD = re.compile(r"^\s*(?:until|till|for)\s+(?:the\s+)?(?:morning\s+)?(?:brief|digest|morning)\b[\s,.;:-]*", re.I)
YES = re.compile(r"^(?:yes|yes,? save it|save it|confirm|ok save it)\.?$", re.I)
FORGET = re.compile(r"^(?:forget|delete|remove|undo) rule\s+(\d+)$", re.I)
ANSWERED = re.compile(r"^(?:i\s+)?(?:answered|replied to)\s+(.+?)\.?$", re.I)


def parse_correction(text: str) -> Optional[str]:
    """The reason in a correction, '' when none was given, None when this isn't a correction."""
    raw = " ".join((text or "").split())
    m = _CORRECTION.match(raw)
    if not m:
        return None
    rest = raw[m.end():].lstrip(" ,.;:-")
    rest = _LEAD.sub("", rest).strip()
    return rest[:300]


def propose_rule(row, settings: dict, reason: str, today: str) -> tuple[Optional[dict], str]:
    """A rule that sends alerts like ``row`` to the morning brief instead of interrupting."""
    if row is None:
        return None, "I don't have a recent alert to attach that to, so nothing changed."
    cat, person = row["category"], row["person"] or ""
    ai = settings.get("always_interrupt", {})
    if cat in (ai.get("categories") or []):
        label = CATEGORY_LABELS.get(cat, cat)
        return None, (f"{label.capitalize()} is set to always interrupt, so a rule can't hold it back. "
                      f"To change that: /notify stop always interrupting for {label}")
    if person and person.lower() in {n.lower() for n in ai.get("people") or []}:
        return None, (f"{person} is set to always interrupt. To change that: "
                      f"/notify stop always interrupting for {person}")
    watch = ""
    if row["source"] == "watch" and str(row["key"]).startswith("watch:"):
        watch = str(row["key"]).split(":")[1]
    rule = {"category": cat, "person": person, "watch": watch, "level": "digest", "reason": reason,
            "added": today, "from_alert": int(row["id"])}
    preview = describe_rule(len(settings.get("rules") or []) + 1, rule)
    floor = " (Money and approval sends stay in the brief at least; they never go quieter.)" if cat in FLOORS else ""
    nudge = "" if reason else (" No reason given; a reason helps the desk tell similar alerts apart, e.g. "
                               "/notify that could have waited, vendor news only matters when a date changes.")
    return rule, (f"About alert #{row['id']} ({row['title'][:80]}). I'd save this rule: {preview}{floor}{nudge}\n"
                  f"Reply /notify yes to save it. Anything else leaves your settings as they are.")


def save_rule(rule: dict, settings: dict) -> tuple[dict, str]:
    s = copy.deepcopy(settings)
    clean = {k: v for k, v in rule.items() if k != "from_alert"}
    s["rules"].append(clean)
    s = _normalize_or_explain(s)
    n = len(s["rules"])
    return s, f"Saved. {describe_rule(n, s['rules'][-1])} Undo: /notify forget rule {n}"


def forget_rule(n: int, settings: dict) -> tuple[dict, str]:
    s = copy.deepcopy(settings)
    if not 1 <= n <= len(s["rules"]):
        raise SettingsError(f"there's no rule {n}; /notify shows your saved rules")
    gone = s["rules"].pop(n - 1)
    s = _normalize_or_explain(s)
    why = f', "{gone["reason"]}"' if gone.get("reason") else ""
    return s, (f"Forgot rule {n}{why}. Those alerts follow their normal level again. "
               f"Rules after it moved up one number.")
