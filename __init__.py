"""front-desk: escalations reach the orchestrator first; it decides whether the boss is told.

Thin Hermes wiring only. All logic lives in ``front_desk/`` and is tested without Hermes.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import threading
import time
from pathlib import Path
from typing import Any, Optional

from .front_desk import notify_cmd, settings as settings_mod
from .front_desk.desk import Desk
from .front_desk.policy import Event, classify, match_person
from .front_desk.presence import detect_viewers_fn
from .front_desk.settings import CATEGORIES
from .front_desk.store import Store

log = logging.getLogger("front_desk")
PLUGIN_ID = "front-desk"
POLL_MIN_S = 20.0


def _plugin_session():
    try:
        from hermes_cli import plugin_session
        return plugin_session
    except Exception:
        return None


def _home() -> Path:
    try:
        from hermes_constants import get_hermes_home
        return Path(get_hermes_home())
    except Exception:
        return Path(os.environ.get("HERMES_HOME", "~/.hermes")).expanduser()


def _data_dir() -> Path:
    try:
        from plugins.plugin_storage import plugin_data_dir
        return Path(plugin_data_dir(PLUGIN_ID))
    except Exception:
        d = _home() / "plugin-data" / PLUGIN_ID
        d.mkdir(parents=True, exist_ok=True)
        return d


def _kanban_db() -> Optional[Path]:
    try:
        from hermes_cli.kanban_db import kanban_db_path
        return Path(kanban_db_path())
    except Exception:
        p = _home() / "kanban.db"
        return p if p.exists() else None


class Plugin:
    def __init__(self, ctx: Any):
        self.ctx = ctx
        self.ps = _plugin_session()
        self.viewers_fn = detect_viewers_fn(self.ps, ctx)
        data = _data_dir()
        custom = ctx.get_config("settings_path", "") if hasattr(ctx, "get_config") else ""
        self.settings_path = Path(custom).expanduser() if custom else data / "settings.yaml"
        self._settings_cache: tuple[float, dict] = (-1.0, settings_mod.normalize({}))
        self.desk = Desk(
            store=Store(data / "desk.db"), settings=self.settings, deliver=self.deliver,
            kanban_db=_kanban_db(), cron_jobs=_home() / "cron" / "jobs.json",
            state_db=_home() / "state.db", full_presence=self.viewers_fn is not None,
        )
        self._pump_lock = threading.Lock()
        self._last_pump = 0.0

    # -- settings: reread when the file changes; a broken file keeps the last good one
    def settings(self) -> dict:
        try:
            mtime = self.settings_path.stat().st_mtime if self.settings_path.exists() else 0.0
        except OSError:
            return self._settings_cache[1]
        if mtime != self._settings_cache[0]:
            try:
                self._settings_cache = (mtime, settings_mod.load(self.settings_path))
            except Exception as exc:
                log.warning("front-desk: settings file not loaded (%s); keeping the last good settings", exc)
                self._settings_cache = (mtime, self._settings_cache[1])
        return self._settings_cache[1]

    # -- delivery: only ever the user's own session or their configured phone chat
    def deliver(self, text: str, target: dict) -> bool:
        try:
            if "session_key" in target:
                return bool(self.ctx.inject_message(text, role="user", session_key=target["session_key"]))
            if "origin" in target:
                return bool(self.ctx.inject_message(text, role="user", origin=target["origin"]))
        except Exception:
            log.warning("front-desk: delivery failed", exc_info=True)
        return False

    # -- the loop, off the hook path
    def kick(self, force: bool = False) -> None:
        now = time.time()
        if not force and now - self._last_pump < POLL_MIN_S:
            return
        if not self._pump_lock.acquire(blocking=False):
            return
        self._last_pump = now

        def run():
            try:
                self.desk.pump()
            except Exception:
                log.warning("front-desk: pump failed", exc_info=True)
            finally:
                self._pump_lock.release()

        threading.Thread(target=run, name="front-desk-pump", daemon=True).start()

    # -- context about the calling session
    def _session(self) -> tuple[str, str]:
        key = surface = ""
        try:
            key = self.ps.session_key() if self.ps else ""
        except Exception:
            pass
        try:
            surface = self.ctx.session_surface()
        except Exception:
            pass
        return key or "", surface or ""

    def _automated(self, platform: str = "") -> bool:
        if os.environ.get("HERMES_KANBAN_TASK"):
            return True
        if platform in ("cron", "kanban", "subagent"):
            return True
        try:
            if getattr(self.ctx, "current_cron_execution", None):
                return True
        except Exception:
            pass
        ps = self.ps
        try:
            fn = getattr(ps, "turn_origin", None)
            if callable(fn) and fn() is not None:
                return True  # a process notification or delegation result, not a person
            child = getattr(ps, "in_delegated_child", None)
            if callable(child) and child():
                return True
        except Exception:
            pass
        return False

    def _viewers(self) -> Optional[list]:
        if self.viewers_fn is None:
            return None
        try:
            v = self.viewers_fn()
            return list(v) if isinstance(v, (list, tuple)) else None
        except Exception:
            return None

    # -- hooks
    def on_pre_llm_call(self, **kw: Any) -> Optional[dict]:
        try:
            key, surface = self._session()
            surface = surface or str(kw.get("platform") or "")
            automated = self._automated(surface)
            self.desk.note_turn(session_key=key, surface=surface, user_message=str(kw.get("user_message") or ""),
                                viewers=None if automated else self._viewers(), automated=automated)
            self.kick()
            live = self.desk.live_session() or {}
            if automated or not key or live.get("session_key") != key:
                return None
            text = self.desk.context_for_turn()
            return {"context": text} if text else None
        except Exception:
            log.warning("front-desk: pre_llm_call failed open", exc_info=True)
            return None

    def on_kanban_event(self, **kw: Any) -> None:
        self.kick(force=True)

    def on_tick(self, **kw: Any) -> None:
        self.kick()

    def on_gateway_message(self, event: Any = None, **kw: Any) -> None:
        """Observer only: never skips or rewrites a message. Stores no message text."""
        try:
            src = getattr(event, "source", None)
            if src is None:
                return None
            platform = getattr(getattr(src, "platform", ""), "value", None) or str(getattr(src, "platform", ""))
            self.desk.inbound(
                platform=platform,
                sender=str(getattr(src, "user_name", "") or getattr(event, "user_name", "") or ""),
                sender_id=str(getattr(src, "user_id", "") or getattr(event, "user_id", "") or ""),
                chat_id=str(getattr(src, "chat_id", "") or ""),
                message_id=str(getattr(event, "message_id", "") or time.time()),
            )
            self.kick()
        except Exception:
            log.warning("front-desk: inbound observer failed open", exc_info=True)
        return None

    # -- tools
    def tool_escalate(self, args: dict, **kw: Any) -> str:
        cat = str(args.get("category") or "other")
        if cat not in CATEGORIES:
            cat = "other"
        title = str(args.get("title") or "").strip()
        if not title:
            return json.dumps({"ok": False, "error": "title is required"})
        ref = str(args.get("ref") or "")
        key = str(args.get("key") or "") or "tool:" + hashlib.sha256(f"{cat}|{ref}|{title}".encode()).hexdigest()[:16]
        s = self.settings()
        person = str(args.get("person") or "") or match_person(f"{title} {args.get('detail') or ''}", s)
        ev = Event(key=key, category=classify(cat, f"{title} {args.get('detail') or ''}", s), title=title,
                   detail=str(args.get("detail") or "")[:1000], source="tool", ref=ref, person=person)
        eid = self.desk.ingest(ev)
        self.kick(force=True)
        if eid is None:
            return json.dumps({"ok": True, "duplicate": True, "message": "Already at the front desk."})
        return json.dumps({"ok": True, "id": eid, "message": "Handed to the front desk. The orchestrator decides "
                                                            "whether the user is told; do not message them yourself."})

    def tool_triage(self, args: dict, **kw: Any) -> str:
        if self._automated():
            return json.dumps({"ok": False, "error": "Only the orchestrator's own session triages."})
        key, surface = self._session()
        live = self.desk.live_session() or {}
        in_live = bool(key) and key == live.get("session_key") and surface in ("cli", "tui", "desktop", "acp", "local", "gui")
        try:
            eid = int(args.get("event_id"))
        except (TypeError, ValueError):
            return json.dumps({"ok": False, "error": "event_id must be the escalation number"})
        res = self.desk.triage(eid, str(args.get("decision") or ""), str(args.get("note") or ""),
                               urgent=bool(args.get("urgent")), in_live_session=in_live,
                               viewers_now=self._viewers() if in_live else None)
        return json.dumps(res)

    def tool_status(self, args: dict, **kw: Any) -> str:
        rep = self.desk.status_report()
        rep["recent"] = [
            {"id": r["id"], "category": r["category"], "status": r["status"], "title": r["title"]}
            for r in self.desk.store.rows()[-10:]
        ]
        return json.dumps(rep)

    # -- /notify
    def cmd_notify(self, raw_args: str = "", **kw: Any) -> str:
        text = (raw_args or "").strip()
        try:
            current = settings_mod.load(self.settings_path)
        except Exception as exc:
            return f"Your settings file has a problem, so I changed nothing: {exc}\nFile: {self.settings_path}"
        if text.lower() in ("", "show", "settings", "status"):
            return settings_mod.describe(current) + f"\nFile: {self.settings_path}"
        if text.lower() in ("help", "?"):
            return notify_cmd.HELP
        try:
            new, msg = notify_cmd.apply(text, current)
        except (notify_cmd.Unparsed, settings_mod.SettingsError):
            return notify_cmd.HELP
        settings_mod.save(self.settings_path, new)
        return msg


ESCALATE_SCHEMA = {
    "name": "front_desk_escalate",
    "description": ("Hand an escalation to the front desk instead of messaging the user yourself. "
                    "The orchestrator triages it and the user's notification settings decide whether, "
                    "when and where they hear about it."),
    "parameters": {"type": "object", "properties": {
        "category": {"type": "string", "enum": list(CATEGORIES)},
        "title": {"type": "string", "description": "One plain line: what happened and what is needed."},
        "detail": {"type": "string"},
        "ref": {"type": "string", "description": "Card id, job id or thread id, if any."},
        "person": {"type": "string", "description": "A person this is from or about, if any."},
        "key": {"type": "string", "description": "Optional dedupe key; the same key never alerts twice."},
    }, "required": ["category", "title"]},
}
TRIAGE_SCHEMA = {
    "name": "front_desk_triage",
    "description": ("Decide a front-desk escalation: 'handled' (you dealt with it; the user is not told) or "
                    "'tell_boss' (the user's settings pick the channel and timing). Deciding never sends "
                    "anything to anyone else and approves nothing."),
    "parameters": {"type": "object", "properties": {
        "event_id": {"type": "integer"},
        "decision": {"type": "string", "enum": ["handled", "tell_boss"]},
        "note": {"type": "string", "description": "One line for the log, and for the user if they are told."},
        "urgent": {"type": "boolean", "description": "Tell them now even if the category is digest-level."},
    }, "required": ["event_id", "decision"]},
}
STATUS_SCHEMA = {
    "name": "front_desk_status",
    "description": "Front desk counts, whether the user looks present, and the last ten escalations.",
    "parameters": {"type": "object", "properties": {}},
}


def _cli_setup(parser):
    sub = parser.add_subparsers(dest="front_desk_cmd")
    e = sub.add_parser("escalate", help="Hand an escalation to the front desk (for scripts).")
    e.add_argument("--category", default="other", choices=list(CATEGORIES))
    e.add_argument("--title", required=True)
    e.add_argument("--detail", default="")
    e.add_argument("--ref", default="")
    e.add_argument("--key", default="")
    sub.add_parser("status", help="Show counts and presence.")
    sub.add_parser("settings", help="Show the settings in plain English.")


_PLUGIN: Optional[Plugin] = None


def _cli_handler(args):
    p = _PLUGIN
    if p is None:
        print("front-desk is not loaded")
        return 1
    if args.front_desk_cmd == "escalate":
        print(p.tool_escalate({"category": args.category, "title": args.title, "detail": args.detail,
                               "ref": args.ref, "key": args.key}))
    elif args.front_desk_cmd == "status":
        print(p.tool_status({}))
    else:
        print(p.cmd_notify(""))
    return 0


def register(ctx: Any) -> None:
    global _PLUGIN
    p = Plugin(ctx)
    _PLUGIN = p
    ctx.register_hook("pre_llm_call", p.on_pre_llm_call)
    ctx.register_hook("kanban_task_blocked", p.on_kanban_event)
    ctx.register_hook("kanban_task_completed", p.on_kanban_event)
    ctx.register_hook("on_kanban_dispatch_tick", p.on_tick)
    ctx.register_hook("pre_gateway_dispatch", p.on_gateway_message)
    ctx.register_tool(name="front_desk_escalate", toolset="front_desk", schema=ESCALATE_SCHEMA, handler=p.tool_escalate)
    ctx.register_tool(name="front_desk_triage", toolset="front_desk", schema=TRIAGE_SCHEMA, handler=p.tool_triage)
    ctx.register_tool(name="front_desk_status", toolset="front_desk", schema=STATUS_SCHEMA, handler=p.tool_status)
    ctx.register_command("notify", p.cmd_notify, description="Notification settings in plain words",
                         args_hint="<what to change>")
    if hasattr(ctx, "register_cli_command"):
        ctx.register_cli_command("front-desk", "Front desk escalations", _cli_setup, _cli_handler)
