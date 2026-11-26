import unittest

from helpers import DESK_KEY, PHONE_KEY, DeskCase, at, base_settings


class RoutingToLiveSession(DeskCase):
    def test_urgent_item_goes_to_the_live_session_first_not_the_user(self):
        self.at_desk()
        eid = self.desk.ingest(self.ev())
        self.desk.pump()
        self.assertEqual(len(self.sent.to(DESK_KEY)), 1)
        self.assertIn("triage", self.sent.to(DESK_KEY)[0])
        self.assertEqual(self.sent.to(PHONE_KEY), [])
        self.assertEqual(self.desk.store.row(eid)["status"], "pending")

    def test_live_session_is_the_newest_typed_turn(self):
        self.desk.note_turn(session_key="agent:main:local:old", surface="tui", user_message="x")
        self.clock.advance(60)
        self.desk.note_turn(session_key=DESK_KEY, surface="desktop", user_message="y")
        self.assertEqual(self.desk.live_session()["session_key"], DESK_KEY)

    def test_automation_and_our_own_injections_never_become_the_live_session(self):
        self.desk.note_turn(session_key=DESK_KEY, surface="desktop", user_message="y")
        self.desk.note_turn(session_key="agent:main:kanban:w", surface="desktop", automated=True)
        self.desk.note_turn(session_key="agent:main:local:x", surface="desktop",
                            user_message="[front-desk] 1 new escalation(s)")
        self.assertEqual(self.desk.live_session()["session_key"], DESK_KEY)

    def test_no_live_session_means_no_wake_and_the_timeout_takes_over(self):
        self.make_state_db(self.clock() - 3600)  # away
        eid = self.desk.ingest(self.ev())
        self.desk.pump()
        self.assertEqual(self.sent.sent, [])
        self.clock.advance(11 * 60)
        self.desk.pump()
        self.assertEqual(self.desk.store.row(eid)["decided_by"], "timeout")
        self.assertEqual(len(self.sent.to(PHONE_KEY)), 1)

    def test_a_phone_chat_live_session_is_not_woken_for_triage(self):
        self.desk.note_turn(session_key=PHONE_KEY, surface="telegram", user_message="hey")
        self.desk.ingest(self.ev())
        self.desk.pump()
        self.assertEqual(self.sent.sent, [])


class SettingsLevels(DeskCase):
    def test_interrupt_tell_boss_present_is_told_in_this_reply(self):
        self.at_desk()
        eid = self.desk.ingest(self.ev())
        res = self.desk.triage(eid, "tell_boss", "needs your pick", in_live_session=True)
        self.assertEqual((res["status"], res["channel"]), ("delivered", "in_session"))
        self.assertEqual(self.sent.to(PHONE_KEY), [])

    def test_digest_level_waits_for_the_digest(self):
        self.at_desk()
        eid = self.desk.ingest(self.ev(category="card_done", title="Card t_2 is done"))
        self.desk.pump()
        self.assertEqual(self.sent.sent, [])  # not woken: digest level
        self.assertEqual(self.desk.triage(eid, "tell_boss")["status"], "digest")
        self.desk.pump()  # first pump starts the digest clock
        self.clock.t = at(17, 1)
        self.at_desk()  # back at the computer at 5 PM
        self.desk.pump()
        self.assertEqual(len(self.sent.to(DESK_KEY)), 1)
        self.assertIn("Digest", self.sent.to(DESK_KEY)[0])
        self.assertEqual(self.sent.to(PHONE_KEY), [])
        self.assertEqual(self.desk.store.row(eid)["status"], "delivered")

    def test_digest_goes_to_the_phone_when_away_at_digest_time(self):
        self.at_desk()
        eid = self.desk.ingest(self.ev(category="card_done", title="Card t_2 is done"))
        self.desk.triage(eid, "tell_boss")
        self.desk.pump()
        self.clock.t = at(17, 1)  # idle for three hours by now
        self.desk.pump()
        self.assertEqual(len(self.sent.to(PHONE_KEY)), 1)
        self.assertIn("Digest", self.sent.to(PHONE_KEY)[0])

    def test_log_level_is_fyi_for_the_orchestrator_only(self):
        self.cfg = base_settings(levels={"card_done": "log"})
        self.at_desk()
        eid = self.desk.ingest(self.ev(category="card_done"))
        self.desk.pump()
        self.clock.t = at(17, 1)
        self.desk.pump()
        self.assertEqual(self.sent.sent, [])
        self.assertEqual(self.desk.store.row(eid)["status"], "logged")
        self.assertIn(f"#{eid}", self.desk.context_for_turn())

    def test_never_level_is_suppressed_everywhere(self):
        self.cfg = base_settings(levels={"cron_error": "never"})
        self.at_desk()
        eid = self.desk.ingest(self.ev(category="cron_error"))
        self.desk.pump()
        self.assertEqual(self.desk.store.row(eid)["status"], "suppressed")
        self.assertEqual(self.desk.context_for_turn(), "")
        self.assertEqual(self.sent.sent, [])

    def test_handled_means_the_boss_is_never_told(self):
        self.at_desk()
        eid = self.desk.ingest(self.ev())
        self.desk.triage(eid, "handled", "retried, fine")
        self.clock.advance(3600)
        self.desk.pump()
        self.assertEqual(self.sent.to(PHONE_KEY), [])
        self.assertEqual(self.desk.store.row(eid)["status"], "handled")

    def test_untriaged_digest_items_still_reach_the_digest(self):
        self.at_desk()
        self.desk.pump()
        eid = self.desk.ingest(self.ev(category="card_failed"))
        self.clock.t = at(17, 5)
        self.desk.pump()
        self.assertEqual(self.desk.store.row(eid)["status"], "delivered")


class QuietHours(DeskCase):
    def test_urgent_item_during_quiet_hours_waits_for_the_digest(self):
        self.clock.t = at(23)
        self.make_state_db(self.clock() - 3 * 3600)  # asleep
        eid = self.desk.ingest(self.ev())
        self.clock.advance(11 * 60)
        self.desk.pump()
        self.assertEqual(self.sent.sent, [])
        self.assertEqual(self.desk.store.row(eid)["status"], "digest")

    def test_quiet_hours_wrap_midnight(self):
        from front_desk.policy import in_quiet_hours, _hhmm  # noqa: F401
        from datetime import datetime
        from zoneinfo import ZoneInfo
        z = ZoneInfo("America/Chicago")
        self.assertTrue(in_quiet_hours(datetime(2026, 3, 5, 3, 0, tzinfo=z), self.cfg))
        self.assertFalse(in_quiet_hours(datetime(2026, 3, 5, 7, 0, tzinfo=z), self.cfg))
        self.assertTrue(in_quiet_hours(datetime(2026, 3, 4, 22, 0, tzinfo=z), self.cfg))

    def test_quiet_hours_never_hide_news_while_you_are_at_the_desk(self):
        self.clock.t = at(23)
        self.at_desk()
        eid = self.desk.ingest(self.ev())
        res = self.desk.triage(eid, "tell_boss", in_live_session=True)
        self.assertEqual(res["status"], "delivered")

    def test_quiet_hours_off(self):
        self.cfg = base_settings(quiet_hours="off")
        self.clock.t = at(23)
        self.make_state_db(self.clock() - 3 * 3600)
        self.desk.ingest(self.ev())
        self.clock.advance(11 * 60)
        self.desk.pump()
        self.assertEqual(len(self.sent.to(PHONE_KEY)), 1)


class Overrides(DeskCase):
    def test_money_always_interrupts_even_in_quiet_hours_and_even_if_handled(self):
        self.clock.t = at(2)
        self.make_state_db(self.clock() - 5 * 3600)
        eid = self.desk.ingest(self.ev(category="money", title="Refund request on invoice 1001"))
        res = self.desk.triage(eid, "handled", "looked at it")
        self.assertIn("always interrupts", res["message"])
        self.desk.pump()
        self.assertEqual(len(self.sent.to(PHONE_KEY)), 1)
        self.assertEqual(self.desk.store.row(eid)["status"], "delivered")

    def test_named_person_always_interrupts(self):
        self.cfg = base_settings(people=[{"name": "Dana Reyes", "match": ["dana"]}],
                                 always_interrupt={"categories": ["money"], "people": ["Dana Reyes"]},
                                 levels={"inbound_person": "log"})
        self.make_state_db(self.clock() - 3600)
        eid = self.desk.inbound(platform="telegram", sender="Dana", sender_id="555", chat_id="9", message_id="1")
        self.assertIsNotNone(eid)
        self.desk.pump()
        self.assertEqual(len(self.sent.to(PHONE_KEY)), 1)

    def test_unnamed_sender_is_not_an_escalation(self):
        self.assertIsNone(self.desk.inbound(platform="telegram", sender="Someone", sender_id="7",
                                            chat_id="9", message_id="2"))

    def test_override_ignores_a_digest_level_setting(self):
        self.cfg = base_settings(levels={"money": "never"})
        self.make_state_db(self.clock() - 3600)
        eid = self.desk.ingest(self.ev(category="money"))
        self.assertEqual(self.desk.store.row(eid)["status"], "notify")


class PresenceDegraded(DeskCase):
    """v0.23.3: no session_viewers. Presence comes from state.db."""

    def test_present_from_recent_local_input(self):
        self.make_state_db(self.clock() - 120)
        p = self.desk.presence()
        self.assertEqual((p.state, p.mode), ("present", "degraded"))

    def test_away_from_old_local_input(self):
        self.make_state_db(self.clock() - 3600)
        self.assertEqual(self.desk.presence().state, "away")

    def test_unknown_without_state_db(self):
        self.assertEqual(self.desk.presence().state, "unknown")

    def test_our_injected_text_does_not_count_as_the_user(self):
        self.make_state_db(self.clock() - 3600)
        self.make_state_db(self.clock() - 10, content="[front-desk] 1 new escalation(s)")
        self.assertEqual(self.desk.presence().state, "away")

    def test_kanban_and_cron_sessions_do_not_count(self):
        self.make_state_db(self.clock() - 10, source="kanban")
        self.make_state_db(self.clock() - 10, source="cron")
        self.assertEqual(self.desk.presence().state, "unknown")

    def test_unknown_presence_urgent_goes_to_phone_by_default(self):
        eid = self.desk.ingest(self.ev())
        self.clock.advance(11 * 60)
        self.desk.pump()
        self.assertEqual(self.desk.store.row(eid)["channel"], "phone")

    def test_away_urgent_goes_to_phone_only(self):
        self.at_desk(minutes_ago=60)
        eid = self.desk.ingest(self.ev())
        self.desk.triage(eid, "tell_boss", in_live_session=False)
        self.assertEqual(len(self.sent.to(PHONE_KEY)), 1)
        self.assertEqual([t for t in self.sent.to(DESK_KEY) if "Tell the user" in t], [])

    def test_no_phone_chat_set_never_guesses_a_chat(self):
        self.cfg = base_settings(phone={"session_key": ""})
        self.make_state_db(self.clock() - 3600)
        self.desk.note_turn(session_key="agent:main:telegram:dm:someone", surface="telegram", user_message="x")
        self.clock.advance(3600)
        eid = self.desk.ingest(self.ev())
        self.clock.advance(11 * 60)
        self.desk.pump()
        self.assertEqual(self.sent.sent, [])
        self.assertEqual(self.desk.store.row(eid)["status"], "digest")


class PresenceFull(DeskCase):
    """v0.23.4+: session_viewers gives the attached clients and their idle time."""
    full_presence = True

    def test_present_from_viewers(self):
        p = self.desk.presence([{"name": "hermes-desktop", "kind": "desktop", "idle_seconds": 30,
                                 "basis": "system", "can_alert": True}])
        self.assertEqual((p.state, p.mode), ("present", "full"))

    def test_away_when_idle_or_no_viewer(self):
        self.assertEqual(self.desk.presence([{"idle_seconds": 4000}]).state, "away")
        self.assertEqual(self.desk.presence([]).state, "away")

    def test_unknown_when_attached_but_no_input_seen(self):
        self.assertEqual(self.desk.presence([{"name": "x", "idle_seconds": None}]).state, "unknown")

    def test_screen_locked_is_away(self):
        self.assertEqual(self.desk.presence([{"idle_seconds": 5, "away": True}]).state, "away")

    def test_snapshot_ages_when_deciding_from_a_timer(self):
        self.desk.note_turn(session_key=DESK_KEY, surface="desktop", user_message="hi",
                            viewers=[{"idle_seconds": 5}])
        self.assertEqual(self.desk.presence().state, "present")
        self.clock.advance(20 * 60)
        self.assertEqual(self.desk.presence().state, "away")

    def test_full_mode_beats_state_db(self):
        self.make_state_db(self.clock() - 5)  # degraded source says present
        self.desk.note_turn(session_key=DESK_KEY, surface="desktop", user_message="hi",
                            viewers=[{"idle_seconds": 4000}])
        self.assertEqual(self.desk.presence().mode, "full")
        self.assertEqual(self.desk.presence().state, "away")


class Dedupe(DeskCase):
    def test_same_escalation_twice_is_one_row(self):
        self.assertIsNotNone(self.desk.ingest(self.ev(key="same")))
        self.assertIsNone(self.desk.ingest(self.ev(key="same")))
        self.assertEqual(len(self.desk.store.rows()), 1)

    def test_one_escalation_never_alerts_twice(self):
        self.make_state_db(self.clock() - 3600)
        self.desk.note_turn(session_key=DESK_KEY, surface="desktop", user_message="hi")
        self.clock.advance(3600)
        eid = self.desk.ingest(self.ev())
        self.desk.ingest(self.ev())  # duplicate intake
        for _ in range(3):
            self.desk.pump()
        self.assertEqual(len(self.sent.to(DESK_KEY)), 1)  # one wake
        self.desk.triage(eid, "tell_boss")
        self.desk.triage(eid, "tell_boss")  # second decision refused
        self.clock.advance(3600)
        for _ in range(3):
            self.desk.pump()
        self.assertEqual(len(self.sent.to(PHONE_KEY)), 1)

    def test_refused_delivery_retries_without_doubling(self):
        self.sent.accept = False
        self.make_state_db(self.clock() - 3600)
        eid = self.desk.ingest(self.ev(category="money"))
        self.desk.pump()
        self.assertEqual(self.desk.store.row(eid)["status"], "notify")
        self.sent.accept = True
        self.desk.pump()
        self.desk.pump()
        self.assertEqual(self.desk.store.row(eid)["status"], "delivered")
        self.assertEqual(len(self.sent.to(PHONE_KEY)), 2)  # one refused + one accepted

    def test_three_refusals_fall_back_to_the_digest(self):
        self.sent.accept = False
        self.make_state_db(self.clock() - 3600)
        eid = self.desk.ingest(self.ev(category="money"))
        for _ in range(4):
            self.desk.pump()
        self.assertEqual(self.desk.store.row(eid)["status"], "digest")
        self.assertEqual(len(self.sent.to(PHONE_KEY)), 3)


class ContextForTurn(DeskCase):
    def test_pending_items_are_shown_then_not_repeated_immediately(self):
        self.at_desk()
        eid = self.desk.ingest(self.ev())
        self.assertIn(f"#{eid}", self.desk.context_for_turn())
        self.assertEqual(self.desk.context_for_turn(), "")
        self.clock.advance(601)
        self.assertIn(f"#{eid}", self.desk.context_for_turn())



class ReviewRegressions(DeskCase):
    def test_a_gateway_chat_never_becomes_the_live_session(self):
        # A client's DM answered by the orchestrator must never receive the boss's notices.
        self.desk.note_turn(session_key="agent:main:telegram:dm:client-1", surface="telegram", user_message="hi")
        self.assertIsNone(self.desk.live_session())
        self.desk.ingest(self.ev(category="card_done"))
        self.desk.store.put("last_digest", self.clock() - 1)
        self.clock.t = at(17, 1)
        self.desk.pump()  # presence unknown -> digest channel in_session, but there is no local session
        self.assertEqual([tg for _, tg in self.sent.sent if tg.get("session_key", "").endswith("client-1")], [])
        self.assertEqual(self.desk.store.rows()[0]["status"], "digest")  # kept for later, never dropped

    def test_in_session_needs_a_local_live_session(self):
        self.desk.store.put("live", {"session_key": "agent:main:telegram:dm:client-1", "surface": "telegram", "ts": self.clock()})
        self.assertIsNone(self.desk._target("in_session", self.desk.live_session()))

    def test_own_phone_chat_means_away(self):
        self.at_desk(minutes_ago=0)
        self.clock.advance(60)
        self.desk.note_turn(session_key=PHONE_KEY, surface="telegram", user_message="on my phone")
        self.assertEqual(self.desk.live_session()["session_key"], DESK_KEY)
        self.assertEqual(self.desk.presence().state, "away")

    def test_bad_timezone_is_refused(self):
        from front_desk import settings as settings_mod
        with self.assertRaises(settings_mod.SettingsError):
            settings_mod.normalize({"timezone": "Americas/Chicago"})

    def test_lost_claim_becomes_unconfirmed_and_is_never_resent(self):
        self.make_state_db(self.clock() - 3600)
        eid = self.desk.ingest(self.ev(category="money"))
        self.desk.store.transition(eid, ("notify",), status="sending", notified_at=self.clock(), channel="phone")
        self.clock.advance(11 * 60)
        self.desk.pump()
        self.assertEqual(self.desk.store.row(eid)["status"], "unconfirmed")
        self.assertEqual(self.sent.sent, [])
        self.assertIn("unconfirmed", self.desk.context_for_turn())

    def test_route_without_a_phone_chat_is_none(self):
        from front_desk import policy
        cfg = base_settings(phone={"session_key": ""})
        self.assertEqual(policy.route("interrupt", "away", False, self.desk._now_dt(self.clock()), cfg), ("interrupt", "none"))
        self.assertEqual(policy.route("interrupt", "away", False, self.desk._now_dt(self.clock()), self.cfg), ("interrupt", "phone"))

if __name__ == "__main__":
    unittest.main()
