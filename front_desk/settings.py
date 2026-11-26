"""Notification settings: the file the user owns.

Plain data in, plain data out. No Hermes imports, so it is testable on its own.
"""
from __future__ import annotations

import copy
import os
import re
import tempfile
from pathlib import Path
from typing import Any

LEVELS = ("interrupt", "digest", "log", "never")
CHANNELS = ("in_session", "phone", "none")
PRESENCE_STATES = ("present", "away", "unknown")

CATEGORIES = (
    "client_escalation",
    "money",
    "deploy",
    "card_needs_input",
    "card_failed",
    "card_done",
    "cron_error",
    "inbound_person",
    "other",
)

CATEGORY_LABELS = {
    "client_escalation": "client escalations",
    "money": "money",
    "deploy": "deploys and live changes",
    "card_needs_input": "cards waiting on a decision",
    "card_failed": "failed or stuck cards",
    "card_done": "finished cards",
    "cron_error": "scheduled job errors",
    "inbound_person": "messages from people you named",
    "other": "anything else",
}

LEVEL_LABELS = {
    "interrupt": "interrupt me now",
    "digest": "put it in the next digest",
    "log": "log it, don't tell me",
    "never": "ignore it completely",
}

DEFAULTS: dict[str, Any] = {
    "levels": {
        "client_escalation": "interrupt",
        "money": "interrupt",
        "deploy": "interrupt",
        "card_needs_input": "interrupt",
        "card_failed": "digest",
        "card_done": "digest",
        "cron_error": "digest",
        "inbound_person": "digest",
        "other": "digest",
    },
    "always_interrupt": {"categories": ["money"], "people": []},
    "people": [],
    "channels": {
        "interrupt": {"present": "in_session", "away": "phone", "unknown": "phone"},
        "digest": {"present": "in_session", "away": "phone", "unknown": "in_session"},
    },
    "phone": {"session_key": ""},
    "quiet_hours": {"start": "22:00", "end": "07:00"},
    "timezone": "",
    "digest_times": ["09:00", "17:00"],
    "presence": {"present_idle_minutes": 10},
    "triage": {"orchestrator_first": True, "timeout_minutes": 10},
    "keywords": {
        "money": ["payment", "invoice", "refund", "chargeback", "payout", "billing", "stripe"],
        "deploy": ["deploy", "production", "go live", "dns", "rollback", "release"],
        "client_escalation": ["client escalation", "customer escalation", "escalat"],
    },
}

_HHMM = re.compile(r"^([01]?\d|2[0-3]):([0-5]\d)$")


class SettingsError(ValueError):
    pass


def _merge(base: dict, over: dict) -> dict:
    out = copy.deepcopy(base)
    for k, v in (over or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _merge(out[k], v)
        else:
            out[k] = copy.deepcopy(v)
    return out


def normalize(raw: dict | None) -> dict:
    """Defaults + user values, validated. Unknown values raise SettingsError."""
    s = _merge(DEFAULTS, raw or {})
    for cat, lvl in list(s["levels"].items()):
        if cat not in CATEGORIES:
            raise SettingsError(f"unknown category {cat!r}")
        if lvl not in LEVELS:
            raise SettingsError(f"{cat}: level must be one of {', '.join(LEVELS)}")
    for urg in ("interrupt", "digest"):
        for state in PRESENCE_STATES:
            ch = s["channels"].get(urg, {}).get(state)
            if ch not in CHANNELS:
                raise SettingsError(f"channels.{urg}.{state} must be one of {', '.join(CHANNELS)}")
    qh = s.get("quiet_hours")
    if qh in (None, False, "off"):
        s["quiet_hours"] = None
    else:
        for key in ("start", "end"):
            if not _HHMM.match(str(qh.get(key, ""))):
                raise SettingsError(f"quiet_hours.{key} must look like 22:00")
    for t in s["digest_times"]:
        if not _HHMM.match(str(t)):
            raise SettingsError(f"digest time {t!r} must look like 09:00")
    for c in s["always_interrupt"].get("categories", []):
        if c not in CATEGORIES:
            raise SettingsError(f"always_interrupt: unknown category {c!r}")
    people = []
    for p in s.get("people") or []:
        if isinstance(p, str):
            p = {"name": p}
        name = str(p.get("name", "")).strip()
        if not name:
            raise SettingsError("each person needs a name")
        match = [str(m).lower() for m in (p.get("match") or [])] or [name.lower()]
        people.append({"name": name, "match": match})
    s["people"] = people
    return s


def load(path: Path) -> dict:
    """Read the YAML file; missing file = defaults. A broken file raises (fail loud, not silent)."""
    if not path.exists():
        return normalize({})
    import yaml  # Hermes ships PyYAML

    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if not isinstance(raw, dict):
        raise SettingsError("settings file must be a mapping")
    return normalize(raw)


def save(path: Path, settings: dict) -> None:
    """Atomic write, keeping the previous file as .bak."""
    import yaml

    normalize(settings)  # never write something load() would reject
    path.parent.mkdir(parents=True, exist_ok=True)
    text = HEADER + yaml.safe_dump(settings, sort_keys=False, allow_unicode=True)
    if path.exists():
        path.with_suffix(path.suffix + ".bak").write_text(path.read_text(encoding="utf-8"), encoding="utf-8")
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".settings-")
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(text)
    os.replace(tmp, path)


HEADER = """# Front desk notification settings. You own this file; /notify edits it in plain words.
# levels: interrupt (tell me now) | digest (next summary) | log (record, don't tell me) | never
# channels: in_session (the chat you're in) | phone (your own gateway chat) | none
"""


def describe(s: dict) -> str:
    """The settings in plain English, for /notify with no arguments."""
    lines = ["Front desk settings:"]
    for cat in CATEGORIES:
        lines.append(f"- {CATEGORY_LABELS[cat]}: {LEVEL_LABELS[s['levels'][cat]]}")
    always = [CATEGORY_LABELS[c] for c in s["always_interrupt"].get("categories", [])]
    always += list(s["always_interrupt"].get("people", []))
    if always:
        lines.append("Always interrupt, even in quiet hours: " + ", ".join(always))
    qh = s.get("quiet_hours")
    lines.append(f"Quiet hours: {qh['start']} to {qh['end']}" if qh else "Quiet hours: off")
    lines.append("Digest times: " + (", ".join(s["digest_times"]) or "none"))
    phone = "set" if (s["phone"].get("session_key") or s["phone"].get("origin")) else "not set (no phone alerts)"
    lines.append(f"Phone chat: {phone}")
    ch = s["channels"]
    lines.append(
        f"At the computer: urgent -> {ch['interrupt']['present']}. Away: urgent -> {ch['interrupt']['away']}."
    )
    return "\n".join(lines)
