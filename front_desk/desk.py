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

from . import policy, presence as presence_mod, sources, watches
from .policy import Event
from .presence import MARKER, Presence
from .settings import CATEGORY_LABELS, FLOORS
from .store import Store

LOCAL_SURFACES = {"cli", "tui", "desktop", "acp", "local", "gui"}
SHOW_AGAIN_S = 600
MAX_ATTEMPTS = 3
LOST_CLAIM_S = 600
BRIEF_MAX = 25     # digest items per brief; the rest wait for the next one, never dropped
WAITING_MAX = 15   # cards listed under "Waiting on you"; the rest are counted

Deliver = Callable[[str, dict], bool]  # (text, {"session_key"} | {"origin"}) -> accepted


class Board:
    """Optional Kanban writer for watch cards. The plugin supplies a real one when the host has
    the Kanban API; tests supply a fake. Desk code never touches the board's SQLite directly."""

    def ensure_card(self, watch: dict) -> Optional[str]:  # returns a card id
        raise NotImplementedError

    def comment(self, card_id: str, body: str) -> None:
        raise NotImplementedError


class Desk:
    def __init__(self, *, store: Store, settings: Callable[[], dict], deliver: Deliver,
                 kanban_db: Optional[Path] = None, cron_jobs: Optional[Path] = None,
                 state_db: Optional[Path] = None, full_presence: bool = False,
                 clock: Callable[[], float] = time.time, board: Optional[Board] = None):
        self.store, self._settings, self._deliver = store, settings, deliver
        self.kanban_db, self.cron_jobs, self.state_db = kanban_db, cron_jobs, state_db
        self.full_presence, self.clock, self.board = full_presence, clock, board

    # ------------------------------------------------------------------ helpers
    @property
    def settings(self) -> dict:
        return self._settings()

    def _now_dt(self, t: float) -> datetime:
        tzname = self.settings.get("timezone") or ""
        try:
            tz = ZoneInfo(tzname) if tzname else None
        except Exception:
            tz = None  # normalize() refuses bad names; this only guards a hand-built dict
        return datetime.fromtimestamp(t, tz or datetime.now().astimezone().tzinfo)

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
        if surface not in LOCAL_SURFACES:
            # A gateway chat is never the live session: it could be anyone's chat. Only the
            # user's own configured phone chat counts, and only as "they're on their phone".
            phone = policy.phone_target(self.settings) or {}
            if phone.get("session_key") == session_key:
                self.store.put("phone_seen", now)
            return
        self.store.put("live", {"session_key": session_key, "surface": surface, "ts": now})
        if viewers is not None:
            self.store.put("viewers", {"ts": now, "viewers": viewers, "session_key": session_key})

    def presence(self, viewers_now: Optional[list] = None) -> Presence:
        s = self.settings
        limit = float(s["presence"]["present_idle_minutes"]) * 60
        now = self.clock()
        live = self.live_session()
        phone_seen = self.store.get("phone_seen")
        if phone_seen and now - float(phone_seen) <= limit and float(phone_seen) > float((live or {}).get("ts", 0)):
            # The user's newest typed turn came from their own phone chat: not at the computer.
            return Presence("away", "full" if self.full_presence else "degraded", "chatting from their phone")
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
        hit = None if override else policy.rule_for(ev, s)
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
        if eid is not None and hit:
            n, rule = hit
            why = f": {rule['reason']}" if rule.get("reason") else ""
            self.store.transition(eid, (status,), note=f"boss's rule {n}{why}")
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
                self._count_alert(row["id"])
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
        out = {"ingested": self.poll()}
        out["watched"] = self.run_check_if_due()
        out.update(woke=self.wake(), timed_out=self.apply_timeouts(), unconfirmed=self.reap_lost_claims())
        out["delivered"] = self.deliver_due()
        out["brief"] = self.send_brief_if_due()
        out["digest"] = self.send_digest_if_due()
        return out

    # ------------------------------------------------------------------ rhythm: checks
    def run_check_if_due(self) -> int:
        """Every ``check_every_hours``: evaluate the standing watches. A check sends nothing by
        itself; a breach becomes an escalation like any other (deduped per breach), so the boss
        hears only when something needs them. Returns how many new escalations it raised."""
        s = self.settings
        hours = float(s.get("check_every_hours") or 0)
        now = self.clock()
        last = self.store.get("last_check")
        if last is None:
            self.store.put("last_check", now)  # start counting from install
            return 0
        if hours <= 0 or now - float(last) < hours * 3600:
            return 0
        self.store.put("last_check", now)
        return self.check_now()

    def check_now(self) -> int:
        s = self.settings
        now = self.clock()
        if not s.get("responsibilities"):
            return 0
        blocked = sources.blocked_on_boss(self.kanban_db) if self.kanban_db is not None else []
        inbound = [dict(r) for r in self.store.rows("category='inbound_person'")]
        answered = self.store.get("answered") or {}
        new = 0
        for w in s["responsibilities"]:
            card = self._watch_card(w) if w.get("card") else None
            # A reply watch reports each wait once; the next unanswered message starts a new wait.
            marks_key = f"reply_marks:{w['id']}"
            marks = self.store.get(marks_key) or {}
            seen = {k: max(float(answered.get(k, 0)), float(marks.get(k, 0))) for k in {*answered, *marks}}
            for ev in watches.evaluate(w, now, blocked_cards=blocked, inbound=inbound, answered=seen):
                eid = self.ingest(ev)
                if ev.since and ev.person:
                    marks[ev.person.lower()] = max(float(marks.get(ev.person.lower(), 0)), ev.since)
                    self.store.put(marks_key, marks)
                if eid is None:
                    continue  # this breach already alerted
                new += 1
                if card and self.board is not None:
                    try:
                        self.board.comment(card, f"Breached: {ev.title} (front desk #{eid})")
                    except Exception:
                        pass  # the board is a mirror; the escalation itself is already recorded
        return new

    def _watch_card(self, w: dict) -> Optional[str]:
        """The board card that shows a watch, made once (opt-in per watch with ``card: true``)."""
        if self.board is None:
            return None
        key = f"watch_card:{w['id']}"
        have = self.store.get(key)
        if have:
            return have
        try:
            cid = self.board.ensure_card(w)
        except Exception:
            return None
        if cid:
            self.store.put(key, cid)
        return cid

    def mark_answered(self, person: str) -> str:
        names = {p["name"].lower(): p["name"] for p in self.settings.get("people", [])}
        name = names.get(person.strip().lower())
        if not name:
            return ""
        answered = self.store.get("answered") or {}
        answered[name.lower()] = self.clock()
        self.store.put("answered", answered)
        return name

    # ------------------------------------------------------------------ rhythm: the brief
    def _count_alert(self, event_id: int) -> None:
        day = self._now_dt(self.clock()).date().isoformat()
        self.store.put(f"alerts:{day}", int(self.store.get(f"alerts:{day}", 0)) + 1)
        self.store.put("last_alert", int(event_id))

    def alert_count(self, days_back: int = 0) -> int:
        from datetime import timedelta
        day = (self._now_dt(self.clock()).date() - timedelta(days=days_back)).isoformat()
        return int(self.store.get(f"alerts:{day}", 0))

    def last_alert(self, max_age_s: float = 86400) -> Optional[Any]:
        """The newest alert that interrupted the boss, if it is under a day old."""
        eid = self.store.get("last_alert")
        row = self.store.row(int(eid)) if eid is not None else None
        if row is None or row["notified_at"] is None or self.clock() - float(row["notified_at"]) > max_age_s:
            return None
        return row

    def build_brief(self, rows: Optional[list] = None, handled: Optional[list] = None) -> str:
        """The brief's text. ``rows`` = digest items it carries, ``handled`` = money/approval items
        the orchestrator handled (defaults: everything waiting, for a preview). Read-only."""
        now = self.clock()
        when = self._now_dt(now).strftime("%a %b %d").replace(" 0", " ")
        lines = [f"{MARKER} Morning brief, {when}."]
        blocked = self.blocked_on_boss()
        if blocked:
            lines.append(f"Waiting on you ({len(blocked)}):")
            for c in blocked[:WAITING_MAX]:
                name = c["title"] if c["title"].startswith(c["code"]) else f"{c['code']} · {c['title']}"
                lines.append(f"- {name[:100]} (blocked {watches.ago(now - c['since'])})")
            if len(blocked) > WAITING_MAX:
                lines.append(f"- and {len(blocked) - WAITING_MAX} more on the board")
        elif self.kanban_db is not None:
            lines.append("No cards are waiting on you.")
        rows = self.store.rows("status='digest'")[:BRIEF_MAX] if rows is None else rows
        waiting_ids = {c["task_id"] for c in blocked}
        shown = [r for r in rows if not (r["source"] == "kanban" and r["category"] == "card_needs_input"
                                         and r["ref"] in waiting_ids)]  # already listed above
        if shown:
            lines.append(f"For you ({len(shown)}):")
            lines += [_line(r) for r in shown]
        ids = {r["id"] for r in rows}
        left = len([r for r in self.store.rows("status='digest'") if r["id"] not in ids])
        if left > 0:
            lines.append(f"- and {left} more, in the next brief")
        handled = self.handled_floor()[:BRIEF_MAX] if handled is None else handled
        if handled:
            lines.append("Handled for you (money and approvals are always shown):")
            lines += [_line(r) for r in handled]
        more = len(self.handled_floor()) - len(handled)
        if more > 0:
            lines.append(f"- and {more} more handled, in the next brief")
        y, t = self.alert_count(1), self.alert_count(0)
        lines.append(f"Alerts: {y} interruption(s) yesterday, {t} so far today. If one could have waited, "
                     f"say so: /notify that could have waited, <why>")
        return "\n".join(lines)

    def blocked_on_boss(self) -> list:
        return sources.blocked_on_boss(self.kanban_db) if self.kanban_db is not None else []

    def handled_floor(self) -> list:
        """Money and approval items the orchestrator handled that no brief has shown yet."""
        marks = ",".join("?" for _ in FLOORS)
        return self.store.rows(f"status='handled' AND category IN ({marks}) AND briefed_at IS NULL", FLOORS)

    def send_brief_if_due(self) -> int:
        """Once a day at ``brief.time``: what's blocked on the boss, the digest items, and the
        alert count. Returns 1 when a brief went out."""
        b = self.settings.get("brief")
        now = self.clock()
        last = self.store.get("last_brief")
        if last is None:
            self.store.put("last_brief", now)  # start counting from install, never replay
            return 0
        now_dt = self._now_dt(now)
        if not b or not policy.slot_due(b["time"], self._now_dt(float(last)), now_dt):
            return 0
        for r in self.store.rows("status='pending' AND level!='interrupt'"):
            self.store.transition(r["id"], ("pending",), status="digest", decided_by="brief_time")
        pres = self.presence()
        _, channel = policy.route("digest", pres.state, False, now_dt, self.settings)
        target = self._target(channel, self.live_session())
        if target is None:
            return 0  # no channel yet; the brief goes out at the next pass that has one
        handled = self.handled_floor()[:BRIEF_MAX]
        if not (self.store.rows("status='digest'") or handled or self.blocked_on_boss() or self.alert_count(1)):
            self.store.put("last_brief", now)  # nothing at all to say: no brief today
            return 0
        mine = [r for r in self.store.rows("status='digest'")[:BRIEF_MAX]
                if self.store.transition(r["id"], ("digest",), status="sending", channel=channel, notified_at=now)]
        ok = self._send(self.build_brief(mine, handled), target)
        if ok:
            self.store.touch([r["id"] for r in handled], "briefed_at", now)
        for r in mine:
            self.store.transition(r["id"], ("sending",), **({"status": "delivered"} if ok else
                                                           {"status": "digest", "notified_at": None, "channel": ""}))
        if ok:
            self.store.put("last_brief", now)
            self.store.put("last_digest", now)
        return 1 if ok else 0

    def wake(self) -> int:
        """Urgent pending items go to the orchestrator's live session first, once each."""
        live = self.live_session()
        # Only a computer session is woken (note_turn records no other kind; checked again here).
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

    def reap_lost_claims(self) -> int:
        """A delivery claimed but never finished (the process died mid-send) is not resent, since
        it may have arrived. It becomes 'unconfirmed' and the orchestrator is shown it once."""
        cutoff = self.clock() - LOST_CLAIM_S
        n = 0
        for r in self.store.rows("status='sending' AND notified_at<=?", (cutoff,)):
            n += self.store.transition(r["id"], ("sending",), status="unconfirmed", shown_at=None)
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
                self._count_alert(r["id"])
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
        # With no morning brief, the digest is where handled money and approvals are shown.
        handled = self.handled_floor()[:BRIEF_MAX] if self.settings.get("brief") is None else []
        if not rows and not handled:
            self.store.put("last_digest", now)
            return 0
        pres = self.presence()
        _, channel = policy.route("digest", pres.state, False, now_dt, self.settings)
        target = self._target(channel, self.live_session())
        if target is None:
            return 0  # keep them for the next slot that has a channel
        mine = [r for r in rows if self.store.transition(r["id"], ("digest",), status="sending",
                                                         channel=channel, notified_at=now)]
        if not mine and not handled:
            return 0
        text = f"{MARKER} Digest, {len(mine)} item(s):" + "".join("\n" + _line(r) for r in mine)
        if handled:
            text += "\nHandled for you (money and approvals are always shown):" + "".join(
                "\n" + _line(r) for r in handled)
        ok = self._send(text, target)
        if ok:
            self.store.touch([r["id"] for r in handled], "briefed_at", now)
        for r in mine:
            self.store.transition(r["id"], ("sending",), **({"status": "delivered"} if ok else
                                                           {"status": "digest", "notified_at": None, "channel": ""}))
        if ok:
            self.store.put("last_digest", now)
        return (len(mine) + len(handled)) if ok else 0

    # ------------------------------------------------------------------ session context
    def context_for_turn(self) -> str:
        """Text added to the orchestrator's next turn: untriaged items and new FYIs."""
        now = self.clock()
        pending = [r for r in self.store.rows("status='pending'")
                   if r["shown_at"] is None or now - r["shown_at"] >= SHOW_AGAIN_S]
        fyi = self.store.rows("shown_at IS NULL AND (status IN ('logged','unconfirmed') OR "
                              "(override=1 AND status IN ('notify','delivered')))")
        if not pending and not fyi:
            return ""
        lines = ["Front desk (escalations reach you first; you decide whether the boss is told):"]
        if pending:
            lines.append("Waiting on your triage (front_desk_triage: 'handled' or 'tell_boss'):")
            lines += [_line(r) for r in pending[:8]]
        if fyi:
            lines.append("For your information only ('unconfirmed' = a notice to the boss may not have arrived):")
            lines += [_line(r) for r in fyi[:8]]
        self.store.touch([r["id"] for r in pending[:8] + fyi[:8]], "shown_at", now)
        return "\n".join(lines)

    def status_report(self) -> dict:
        counts: dict[str, int] = {}
        for r in self.store.rows():
            counts[r["status"]] = counts.get(r["status"], 0) + 1
        p = self.presence()
        live = self.live_session() or {}
        return {"counts": counts, "alerts_today": self.alert_count(0), "alerts_yesterday": self.alert_count(1),
                "presence": p.state, "presence_mode": p.mode, "basis": p.basis,
                "live_surface": live.get("surface", ""), "phone_set": policy.phone_target(self.settings) is not None}

    # ------------------------------------------------------------------ delivery
    def _target(self, channel: str, live: Optional[dict]) -> Optional[dict]:
        if channel == "in_session":
            if live and live.get("surface") in LOCAL_SURFACES:
                return {"session_key": live["session_key"]}
            return None
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
    if r["status"] == "unconfirmed":
        tag += ", unconfirmed"
    extra = f" ({r['detail']})" if r["detail"] else ""
    note = f" Note: {r['note']}" if r["note"] else ""
    return f"- #{r['id']} [{tag}] {r['title']}{extra}{note}"


def _notice(r: Any, channel: str) -> str:
    if channel == "phone":
        return (f"{MARKER} Notice for the user only. Relay it as written and take no other action: "
                f"{r['title']}{'. ' + r['note'] if r['note'] else ''}")
    return (f"{MARKER} Tell the user now, in one or two plain lines, then carry on: "
            f"{r['title']}{'. ' + r['note'] if r['note'] else ''}")
