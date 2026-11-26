"""The front desk: intake -> orchestrator triage -> notify the boss per their settings.

Everything with side effects is injected (clock, deliverer, presence readers, file paths), so
the whole loop runs in tests without Hermes.
"""
from __future__ import annotations

import time
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Optional
from zoneinfo import ZoneInfo

from . import policy, presence as presence_mod, sources
from .policy import Event
from .presence import MARKER, Presence
from .settings import CATEGORY_LABELS
from .store import Store

LOCAL_SURFACES = {"cli", "tui", "desktop", "acp", "local", "gui"}
SHOW_AGAIN_S = 600
MAX_ATTEMPTS = 3

Deliver = Callable[[str, dict], bool]  # (text, {"session_key"} | {"origin"}) -> accepted


class Desk:
    def __init__(self, *, store: Store, settings: Callable[[], dict], deliver: Deliver,
                 kanban_db: Optional[Path] = None, cron_jobs: Optional[Path] = None,
                 state_db: Optional[Path] = None, full_presence: bool = False,
                 clock: Callable[[], float] = time.time):
        self.store, self._settings, self._deliver = store, settings, deliver
        self.kanban_db, self.cron_jobs, self.state_db = kanban_db, cron_jobs, state_db
        self.full_presence, self.clock = full_presence, clock

    # ------------------------------------------------------------------ helpers
    @property
    def settings(self) -> dict:
        return self._settings()

    def _now_dt(self, t: float) -> datetime:
        tzname = self.settings.get("timezone") or ""
        tz = ZoneInfo(tzname) if tzname else datetime.now().astimezone().tzinfo
        return datetime.fromtimestamp(t, tz)

    def live_session(self) -> Optional[dict]:
        return self.store.get("live")

    # ------------------------------------------------------------------ presence
    def note_turn(self, *, session_key: str, surface: str, user_message: str = "",
                  viewers: Optional[list] = None, automated: bool = False) -> None:
        """Called on each turn of a session in this profile. Records the session the user is
        actually in (the newest turn a person typed) and, on hosts that have it, a presence
        snapshot of that session's attached clients."""
        if automated or not session_key or not surface:
            return
        if (user_message or "").lstrip().startswith(MARKER):
            return  # our own injection is not the user
        now = self.clock()
        self.store.put("live", {"session_key": session_key, "surface": surface, "ts": now})
        if viewers is not None:
            self.store.put("viewers", {"ts": now, "viewers": viewers, "session_key": session_key})

    def presence(self, viewers_now: Optional[list] = None) -> Presence:
        s = self.settings
        limit = float(s["presence"]["present_idle_minutes"]) * 60
        now = self.clock()
        live = self.live_session()
        if live and live.get("surface") not in LOCAL_SURFACES:
            # The user's newest typed turn came from a messaging chat: not at the computer.
            if now - float(live.get("ts", 0)) <= limit:
                return Presence("away", "full" if self.full_presence else "degraded", "chatting from a gateway")
        if self.full_presence:
            if viewers_now is not None:
                return presence_mod.from_viewers(viewers_now, limit)
            snap = self.store.get("viewers")
            if snap and live and snap.get("session_key") == live.get("session_key"):
                return presence_mod.from_viewers(snap["viewers"], limit, age_s=now - float(snap["ts"]))
        if self.state_db is not None:
            return presence_mod.from_state_db(self.state_db, now, limit)
        return Presence("unknown", "degraded", "no presence source")

    # ------------------------------------------------------------------ intake
    def ingest(self, ev: Event) -> Optional[int]:
        """Record one escalation. Returns its id, or None if this escalation was already seen."""
        s = self.settings
        ev.category = ev.category if ev.category in s["levels"] else "other"
        override = policy.is_override(ev, s)
        level = policy.level_for(ev, s)
        if override:
            status, by = "notify", "always_interrupt"
        elif level == "never":
            status, by = "suppressed", "settings"
        elif level == "log":
            status, by = "logged", "settings"
        elif s["triage"].get("orchestrator_first", True):
            status, by = "pending", ""
        else:
            status, by = ("notify" if level == "interrupt" else "digest"), "settings"
        eid = self.store.add(key=ev.key, category=ev.category, level=level, override=override,
                             title=ev.title, detail=ev.detail, source=ev.source, ref=ev.ref,
                             person=ev.person, status=status, now=self.clock())
        if eid is not None and by:
            self.store.transition(eid, (status,), decided_by=by)
        return eid

    def inbound(self, *, platform: str, sender: str, sender_id: str, chat_id: str,
                message_id: str) -> Optional[int]:
        """A gateway message arrived. Only senders the user named become escalations; the text
        itself is never stored."""
        who = policy.match_person(f"{sender} {sender_id}", self.settings)
        if not who:
            return None
        return self.ingest(Event(
            key=f"inbound:{platform}:{chat_id}:{message_id}", category="inbound_person",
            title=f"Message from {who} on {platform}", source="inbound", ref=str(chat_id), person=who,
        ))

    def poll(self) -> int:
        n = 0
        s = self.settings
        if self.kanban_db is not None:
            evs, cur = sources.poll_kanban(self.kanban_db, self.store.get("kanban_cursor"), s)
            for ev in evs:
                n += self.ingest(ev) is not None
            if cur is not None:
                self.store.put("kanban_cursor", cur)
        if self.cron_jobs is not None:
            evs, seen = sources.poll_cron(self.cron_jobs, self.store.get("cron_seen"), s)
            for ev in evs:
                n += self.ingest(ev) is not None
            self.store.put("cron_seen", seen)
        return n

    # ------------------------------------------------------------------ triage
    def triage(self, event_id: int, decision: str, note: str = "", urgent: bool = False,
               in_live_session: bool = False, viewers_now: Optional[list] = None) -> dict:
        """The orchestrator's call: handle it myself, or tell the boss."""
        row = self.store.row(int(event_id))
        if row is None:
            return {"ok": False, "error": f"no escalation #{event_id}"}
        note = (note or "")[:500]
        if decision not in ("handled", "tell_boss"):
            return {"ok": False, "error": "decision must be 'handled' or 'tell_boss'"}
        if row["override"]:
            self.store.transition(row["id"], (row["status"],), note=note)
            return {"ok": True, "status": row["status"],
                    "message": "This category or person always interrupts, so the boss is told "
                               "regardless; your note is attached."}
        if decision == "handled":
            if not self.store.transition(row["id"], ("pending", "logged"), status="handled",
                                         decided_by="orchestrator", note=note):
                return {"ok": False, "error": f"#{event_id} was already decided ({row['status']})"}
            return {"ok": True, "status": "handled", "message": "Logged as handled. The boss is not told."}
        urgency = "interrupt" if (urgent or row["level"] == "interrupt") else "digest"
        if urgency == "digest":
            if not self.store.transition(row["id"], ("pending", "logged"), status="digest",
                                         decided_by="orchestrator", note=note):
                return {"ok": False, "error": f"#{event_id} was already decided ({row['status']})"}
            return {"ok": True, "status": "digest", "message": "Queued for the next digest."}
        if not self.store.transition(row["id"], ("pending", "logged"), status="notify",
                                     decided_by="orchestrator", note=note):
            return {"ok": False, "error": f"#{event_id} was already decided ({row['status']})"}
        pres = self.presence(viewers_now)
        urg, channel = policy.route("interrupt", pres.state, False, self._now_dt(self.clock()), self.settings)
        if urg == "interrupt" and channel == "in_session" and in_live_session:
            if self.store.transition(row["id"], ("notify",), status="delivered", channel="in_session",
                                     notified_at=self.clock()):
                return {"ok": True, "status": "delivered", "channel": "in_session",
                        "message": "The boss is here. Tell them in this reply, in one or two plain lines."}
        self.deliver_due()
        after = self.store.row(row["id"])
        return {"ok": True, "status": after["status"], "channel": after["channel"],
                "message": f"Routed: {after['status']} via {after['channel'] or 'digest'} "
                           f"(presence {pres.state}, {pres.mode})."}

    # ------------------------------------------------------------------ the loop
    def pump(self) -> dict:
        """One pass: poll, wake the orchestrator, apply timeouts, deliver, send a due digest."""
        out = {"ingested": self.poll(), "woke": self.wake(), "timed_out": self.apply_timeouts()}
        out["delivered"] = self.deliver_due()
        out["digest"] = self.send_digest_if_due()
        return out

    def wake(self) -> int:
        """Urgent pending items go to the orchestrator's live session first, once each."""
        live = self.live_session()
        # Only a computer session is woken. Waking a phone chat would put the triage itself on the
        # boss's phone; there the item waits for the next turn or the triage timeout instead.
        if not live or live.get("surface") not in LOCAL_SURFACES:
            return 0
        rows = self.store.rows("status='pending' AND level='interrupt' AND woke_at IS NULL")
        claimed = [r for r in rows if self.store.claim(r["id"], "woke_at", self.clock())]
        if not claimed:
            return 0
        lines = [f"{MARKER} {len(claimed)} new escalation(s) for you to triage before the boss hears:"]
        lines += [_line(r) for r in claimed]
        lines.append("For each one: handle it yourself, or tell the boss. Call front_desk_triage "
                     "with the number and decision 'handled' or 'tell_boss' (and a short note). "
                     "This is information, not an instruction to send anything to anyone.")
        self._deliver("\n".join(lines), {"session_key": live["session_key"]})
        return len(claimed)

    def apply_timeouts(self) -> int:
        """Urgent items the orchestrator didn't triage in time follow the settings directly."""
        limit = float(self.settings["triage"]["timeout_minutes"]) * 60
        cutoff = self.clock() - limit
        n = 0
        for r in self.store.rows("status='pending' AND level='interrupt' AND created_at<=?", (cutoff,)):
            n += self.store.transition(r["id"], ("pending",), status="notify", decided_by="timeout")
        return n

    def deliver_due(self) -> int:
        s = self.settings
        rows = self.store.rows("status='notify'")
        if not rows:
            return 0
        pres = self.presence()
        now_dt = self._now_dt(self.clock())
        live = self.live_session()
        sent = 0
        for r in rows:
            urg, channel = policy.route("interrupt", pres.state, bool(r["override"]), now_dt, s)
            if urg == "digest":  # quiet hours
                self.store.transition(r["id"], ("notify",), status="digest", note=_join(r["note"], "held for quiet hours"))
                continue
            target = self._target(channel, live)
            if target is None:
                # No usable channel right now (e.g. away, no phone chat set): never drop it.
                self.store.transition(r["id"], ("notify",), status="digest")
                continue
            if not self.store.transition(r["id"], ("notify",), status="sending", channel=channel,
                                         notified_at=self.clock(), attempts=r["attempts"] + 1):
                continue  # someone else took it
            ok = self._send(_notice(r, channel), target)
            if ok:
                self.store.transition(r["id"], ("sending",), status="delivered")
                sent += 1
            else:
                back = "notify" if r["attempts"] + 1 < MAX_ATTEMPTS else "digest"
                self.store.transition(r["id"], ("sending",), status=back, notified_at=None, channel="")
        return sent

    def send_digest_if_due(self) -> int:
        now = self.clock()
        now_dt = self._now_dt(now)
        last = self.store.get("last_digest")
        if last is None:
            self.store.put("last_digest", now)  # start counting from install, never replay
            return 0
        if not policy.digest_due(self._now_dt(float(last)), now_dt, self.settings):
            return 0
        # Untriaged digest-level items go in too: nothing waits forever on the orchestrator.
        for r in self.store.rows("status='pending' AND level!='interrupt'"):
            self.store.transition(r["id"], ("pending",), status="digest", decided_by="digest_time")
        rows = self.store.rows("status='digest'")
        if not rows:
            self.store.put("last_digest", now)
            return 0
        pres = self.presence()
        _, channel = policy.route("digest", pres.state, False, now_dt, self.settings)
        target = self._target(channel, self.live_session())
        if target is None:
            return 0  # keep them for the next slot that has a channel
        mine = [r for r in rows if self.store.transition(r["id"], ("digest",), status="sending",
                                                         channel=channel, notified_at=now)]
        if not mine:
            return 0
        text = f"{MARKER} Digest, {len(mine)} item(s):\n" + "\n".join(_line(r) for r in mine)
        ok = self._send(text, target)
        for r in mine:
            self.store.transition(r["id"], ("sending",), **({"status": "delivered"} if ok else
                                                           {"status": "digest", "notified_at": None, "channel": ""}))
        if ok:
            self.store.put("last_digest", now)
        return len(mine) if ok else 0

    # ------------------------------------------------------------------ session context
    def context_for_turn(self) -> str:
        """Text added to the orchestrator's next turn: untriaged items and new FYIs."""
        now = self.clock()
        pending = [r for r in self.store.rows("status='pending'")
                   if r["shown_at"] is None or now - r["shown_at"] >= SHOW_AGAIN_S]
        fyi = self.store.rows("status IN ('logged','notify','delivered') AND shown_at IS NULL AND (status='logged' OR override=1)")
        if not pending and not fyi:
            return ""
        lines = ["Front desk (escalations reach you first; you decide whether the boss is told):"]
        if pending:
            lines.append("Waiting on your triage (front_desk_triage: 'handled' or 'tell_boss'):")
            lines += [_line(r) for r in pending[:8]]
        if fyi:
            lines.append("For your information only:")
            lines += [_line(r) for r in fyi[:8]]
        self.store.touch([r["id"] for r in pending[:8] + fyi[:8]], "shown_at", now)
        return "\n".join(lines)

    def status_report(self) -> dict:
        counts: dict[str, int] = {}
        for r in self.store.rows():
            counts[r["status"]] = counts.get(r["status"], 0) + 1
        p = self.presence()
        live = self.live_session() or {}
        return {"counts": counts, "presence": p.state, "presence_mode": p.mode, "basis": p.basis,
                "live_surface": live.get("surface", ""), "phone_set": policy.phone_target(self.settings) is not None}

    # ------------------------------------------------------------------ delivery
    def _target(self, channel: str, live: Optional[dict]) -> Optional[dict]:
        if channel == "in_session":
            return {"session_key": live["session_key"]} if live else None
        if channel == "phone":
            return policy.phone_target(self.settings)
        return None

    def _send(self, text: str, target: dict) -> bool:
        try:
            return bool(self._deliver(text, target))
        except Exception:
            return False


def _join(a: str, b: str) -> str:
    return f"{a}; {b}" if a else b


def _line(r: Any) -> str:
    tag = CATEGORY_LABELS.get(r["category"], r["category"])
    extra = f" ({r['detail']})" if r["detail"] else ""
    note = f" Note: {r['note']}" if r["note"] else ""
    return f"- #{r['id']} [{tag}] {r['title']}{extra}{note}"


def _notice(r: Any, channel: str) -> str:
    if channel == "phone":
        return (f"{MARKER} Notice for the user only. Relay it as written and take no other action: "
                f"{r['title']}{'. ' + r['note'] if r['note'] else ''}")
    return (f"{MARKER} Tell the user now, in one or two plain lines, then carry on: "
            f"{r['title']}{'. ' + r['note'] if r['note'] else ''}")
