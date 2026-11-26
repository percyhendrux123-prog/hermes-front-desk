"""Notification settings: the file the user owns.

Plain data in, plain data out. No Hermes imports, so it is testable on its own.
"""
from __future__ import annotations

import copy
import hashlib
import json
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
    "approval",
    "deploy",
    "card_needs_input",
    "card_failed",
    "card_done",
    "cron_error",
    "inbound_person",
    "responsibility",
    "other",
)

# Locked floors: these always reach the boss, at least in the morning brief. No settings
# file, /notify phrase or saved rule can push them below "digest".
FLOORS = ("money", "approval")
FLOOR_WHY = ("money and sends waiting on your approval always reach you, at least in the "
             "morning brief. The lowest they can go is the digest")

CATEGORY_LABELS = {
    "client_escalation": "client escalations",
    "money": "money",
    "approval": "sends waiting on your approval",
    "deploy": "deploys and live changes",
    "card_needs_input": "cards waiting on a decision",
    "card_failed": "failed or stuck cards",
    "card_done": "finished cards",
    "cron_error": "scheduled job errors",
    "inbound_person": "messages from people you named",
    "responsibility": "your standing watches",
    "other": "anything else",
}

LEVEL_LABELS = {
    "interrupt": "interrupt me now",
    "digest": "put it in the morning brief",
    "log": "log it, don't tell me",
    "never": "ignore it completely",
}

DEFAULTS: dict[str, Any] = {
    "levels": {
        "client_escalation": "interrupt",
        "money": "interrupt",
        "approval": "interrupt",
        "deploy": "interrupt",
        "card_needs_input": "interrupt",
        "card_failed": "digest",
        "card_done": "digest",
        "cron_error": "digest",
        "inbound_person": "digest",
        "responsibility": "interrupt",
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
    "brief": {"time": "07:30"},          # the morning brief; "off" turns it off
    "check_every_hours": 3,              # standing watches are checked this often; 0 = never
    "digest_times": [],                  # extra digest slots on top of the brief
    "responsibilities": [],
    "rules": [],
    "presence": {"present_idle_minutes": 10},
    "triage": {"orchestrator_first": True, "timeout_minutes": 10},
    "keywords": {
        "money": ["payment", "invoice", "refund", "chargeback", "payout", "billing", "stripe"],
        "approval": ["waiting on approval", "awaiting approval", "needs approval", "approve the send",
                     "ready to send", "draft for approval"],
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
        check_floor(cat, lvl)
    for urg in ("interrupt", "digest"):
        for state in PRESENCE_STATES:
            ch = s["channels"].get(urg, {}).get(state)
            if ch not in CHANNELS:
                raise SettingsError(f"channels.{urg}.{state} must be one of {', '.join(CHANNELS)}")
            # The digest is the floor's last stop: a digest channel of "none" would leave money
            # and approvals waiting forever (an interrupt with no channel falls back to the digest).
            if urg == "digest" and ch == "none" and FLOORS:
                raise SettingsError(f"channels.digest.{state} can't be 'none': {FLOOR_WHY}, so "
                                    "the digest needs somewhere to go (in_session or phone)")
    qh = s.get("quiet_hours")
    if qh in (None, False, "off"):
        s["quiet_hours"] = None
    else:
        for key in ("start", "end"):
            if not _HHMM.match(str(qh.get(key, ""))):
                raise SettingsError(f"quiet_hours.{key} must look like 22:00")
    if s.get("timezone"):
        try:
            from zoneinfo import ZoneInfo
            ZoneInfo(str(s["timezone"]))
        except Exception:
            raise SettingsError(f"unknown timezone {s['timezone']!r} (try America/Chicago)")
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
    b = s.get("brief")
    if b in (None, False, "off") or (isinstance(b, dict) and b.get("time") in (None, False, "off", "")):
        s["brief"] = None
    else:
        if not isinstance(b, dict) or not _HHMM.match(str(b.get("time", ""))):
            raise SettingsError("brief.time must look like 07:30 (or brief: off)")
        s["brief"] = {"time": str(b["time"])}
    if s["brief"] is None and not s["digest_times"]:
        raise SettingsError("with the morning brief off, set at least one digest time (digest_times), "
                            "otherwise digest items, including money and approvals, would never reach you")
    try:
        hours = float(s.get("check_every_hours") or 0)
    except (TypeError, ValueError):
        raise SettingsError("check_every_hours must be a number of hours (0 = no checks)")
    if hours < 0 or (0 < hours < 0.25):
        raise SettingsError("check_every_hours must be 0 (off) or at least 0.25")
    s["check_every_hours"] = hours
    from . import watches  # late: watches imports policy, which imports this module
    out, seen = [], set()
    for entry in s.get("responsibilities") or []:
        try:
            w = watches.parse(entry, people)
        except watches.WatchError as exc:
            raise SettingsError(f"responsibility not understood: {exc}") from None
        if w["id"] not in seen:
            seen.add(w["id"])
            out.append(w)
    s["responsibilities"] = out
    rules = []
    for r in s.get("rules") or []:
        if not isinstance(r, dict) or r.get("category") not in CATEGORIES:
            raise SettingsError("each rule needs a known category")
        lvl = str(r.get("level") or "digest")
        if lvl not in LEVELS:
            raise SettingsError(f"rule level must be one of {', '.join(LEVELS)}")
        check_floor(r["category"], lvl, what="a rule")
        rules.append({"category": r["category"], "person": str(r.get("person") or ""),
                      "watch": str(r.get("watch") or ""), "level": lvl,
                      "reason": str(r.get("reason") or "")[:300], "added": str(r.get("added") or "")})
    s["rules"] = rules
    return s


def check_floor(category: str, level: str, what: str = "") -> None:
    """Refuse, with the reason, any setting that would hide money or an approval send."""
    if category in FLOORS and level in ("log", "never"):
        label = CATEGORY_LABELS.get(category, category)
        lead = f"{what.capitalize()} can't set {label}" if what else f"{label.capitalize()} can't be set"
        raise SettingsError(f"{lead} to '{level}': {FLOOR_WHY}.")


def load(path: Path) -> dict:
    """Read the YAML file; missing file = defaults. A broken file raises (fail loud, not silent)."""
    if not path.exists():
        return normalize({})
    raw = parse_text(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise SettingsError("settings file must be a mapping")
    return normalize(raw)


class _RuamelSafe:
    """PyYAML's safe_load/safe_dump on ruamel.yaml, with YAML 1.1 rules so `brief: off` still
    reads as off. Hermes itself depends on ruamel.yaml (not PyYAML), so this is the parser an
    installed copy actually has. A fresh parser per call: ruamel instances aren't thread-safe.
    Known difference from PyYAML: bare y/n load as booleans here; save() quotes them."""

    def __init__(self, ruamel_yaml):
        self._mod = ruamel_yaml

    def safe_load(self, text: str):
        y = self._mod.YAML(typ="safe", pure=True)
        y.version = (1, 1)
        return y.load(text)

    def safe_dump(self, data, sort_keys: bool = False, allow_unicode: bool = True) -> str:
        import io
        from ruamel.yaml.resolver import VersionedResolver

        class _Yaml11(VersionedResolver):  # quote "off"/"yes" without a %YAML directive
            @property
            def processing_version(self):
                return (1, 1)

        y = self._mod.YAML(typ="safe", pure=True)
        y.Resolver = _Yaml11
        y.default_flow_style = False
        y.allow_unicode = allow_unicode
        y.sort_base_mapping_type_on_output = sort_keys
        y.indent(mapping=2, sequence=4, offset=2)
        out = io.StringIO()
        y.dump(data, out)
        return out.getvalue()


def _yaml():
    """PyYAML if it's there, else ruamel.yaml (what Hermes ships), else None (JSON only)."""
    try:
        import yaml
        return yaml
    except ImportError:
        pass
    try:
        import ruamel.yaml
        return _RuamelSafe(ruamel.yaml)
    except ImportError:
        return None


def parse_text(text: str):
    """YAML when a YAML parser is there (PyYAML or Hermes's ruamel.yaml). Without either, JSON
    (which is valid YAML) still loads, so the desk and its tests run on a bare Python;
    hand-written YAML then fails loud, with why."""
    y = _yaml()
    if y is not None:
        return y.safe_load(text) or {}
    body = "\n".join(line for line in text.splitlines() if not line.lstrip().startswith("#"))
    if not body.strip():
        return {}
    try:
        return json.loads(body)
    except ValueError:
        raise SettingsError("no YAML parser (PyYAML or ruamel.yaml) is installed here, "
                            "so only JSON-style settings can be read") from None


def dump_text(settings: dict) -> str:
    y = _yaml()
    if y is not None:
        return y.safe_dump(settings, sort_keys=False, allow_unicode=True)
    return json.dumps(settings, indent=2, ensure_ascii=False) + "\n"


def to_file_form(s: dict) -> dict:
    """What gets written back: watches as the user's words, not the parsed form."""
    out = copy.deepcopy(s)
    out["responsibilities"] = [
        w["text"] if (w.get("level", "interrupt") == "interrupt" and not w.get("card")) else
        {"text": w["text"], **({"level": w["level"]} if w.get("level") != "interrupt" else {}),
         **({"card": True} if w.get("card") else {})}
        for w in s.get("responsibilities") or []
    ]
    if out.get("brief") is None:
        out["brief"] = "off"
    return out


def save(path: Path, settings: dict) -> None:
    """Atomic write, keeping the previous file as .bak."""
    normalize(to_file_form(settings))  # never write something load() would reject
    path.parent.mkdir(parents=True, exist_ok=True)
    text = HEADER + dump_text(to_file_form(settings))
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
    b = s.get("brief")
    lines.append(f"Morning brief: {b['time']}" if b else "Morning brief: off")
    hrs = s.get("check_every_hours") or 0
    lines.append(f"Checks: every {hrs:g} hours, silent unless something needs you" if hrs else "Checks: off")
    if s.get("digest_times"):
        lines.append("Extra digest times: " + ", ".join(s["digest_times"]))
    from . import watches
    if s.get("responsibilities"):
        lines.append("Standing watches:")
        lines += [f"  - {watches.describe(w)}" for w in s["responsibilities"]]
    if s.get("rules"):
        lines.append("Saved rules (from your corrections):")
        for i, r in enumerate(s["rules"], start=1):
            lines.append("  " + describe_rule(i, r))
    lines.append(f"Locked: {', '.join(CATEGORY_LABELS[c] for c in FLOORS)} never go below the brief.")
    phone = "set" if (s["phone"].get("session_key") or s["phone"].get("origin")) else "not set (no phone alerts)"
    lines.append(f"Phone chat: {phone}")
    ch = s["channels"]
    lines.append(
        f"At the computer: urgent -> {ch['interrupt']['present']}. Away: urgent -> {ch['interrupt']['away']}."
    )
    return "\n".join(lines)


def describe_rule(n: int, r: dict) -> str:
    who = f" from {r['person']}" if r.get("person") else ""
    what = CATEGORY_LABELS.get(r["category"], r["category"])
    if r.get("watch"):
        what = "this standing watch"
    where = {"digest": "the morning brief", "interrupt": "an interrupt", "log": "the log only",
             "never": "nowhere"}[r["level"]]
    why = f' Reason: "{r["reason"]}"' if r.get("reason") else ""
    return f"{n}. {what.capitalize()}{who} -> {where}.{why}"
