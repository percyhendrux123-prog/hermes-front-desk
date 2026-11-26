"""The two rhythms (morning brief, regular checks), standing watches, corrections with a reason,
locked floors and the alert counter. Synthetic names only."""
import json
import sqlite3
import unittest

from helpers import DESK_KEY, PHONE_KEY, DeskCase, at, base_settings
from front_desk import notify_cmd, policy, settings as settings_mod, sources, watches
from front_desk.desk import Board
from front_desk.policy import Event

HOUR = 3600


def board_db(path, cards):
    """cards: (task_id, title, status, created_by, [(kind, payload, created_at), ...])"""
    con = sqlite3.connect(path)
    con.execute("CREATE TABLE IF NOT EXISTS tasks (id TEXT PRIMARY KEY, title TEXT, assignee TEXT, status TEXT,"
                " created_by TEXT)")
    con.execute("CREATE TABLE IF NOT EXISTS task_events (id INTEGER PRIMARY KEY AUTOINCREMENT, task_id TEXT,"
                " run_id INTEGER, kind TEXT, payload TEXT, created_at INTEGER)")
    for task_id, title, status, by, events in cards:
        con.execute("INSERT OR REPLACE INTO tasks VALUES (?,?,?,?,?)", (task_id, title, "worker-a", status, by))
        for kind, payload, ts in events:
            con.execute("INSERT INTO task_events(task_id, run_id, kind, payload, created_at) VALUES (?,?,?,?,?)",
                        (task_id, 1, kind, json.dumps(payload), int(ts)))
    con.commit()
    con.close()


class FakeBoard(Board):
    def __init__(self):
        self.cards, self.comments = [], []

    def ensure_card(self, watch):
        self.cards.append(watch["text"])
        return f"t_watch{len(self.cards)}"

    def comment(self, card_id, body):
        self.comments.append((card_id, body))


class RhythmCase(DeskCase):
    def setUp(self):
        super().setUp()
        self.board = self.tmp / "kanban.db"
        board_db(self.board, [])
        self.desk.kanban_db = self.board

    def block(self, task_id, title, hours_ago, kind="needs_input", by="worker-profile"):
        board_db(self.board, [(task_id, title, "blocked", by,
                               [("blocked", {"kind": kind, "reason": "pick one"}, self.clock() - hours_ago * HOUR)])])

    def settings(self, **over):
        self.cfg = base_settings(**over)


# --------------------------------------------------------------------------- morning brief
class MorningBrief(RhythmCase):
    def start(self, day=4, hh=6):
        self.clock.t = at(hh, 0, day=day)
        self.desk.pump()  # first pass starts the clocks; nothing is replayed

    def test_brief_lists_cards_blocked_on_the_boss_by_code_and_the_alert_count(self):
        self.settings(digest_times=[])
        self.start()
        self.block("t_aa11", "A101 · Pick a launch date", hours_ago=3)
        self.block("t_bb22", "Untagged card", hours_ago=1)
        self.block("t_cc33", "A103 · Waits on parent", hours_ago=5, kind="dependency")
        self.block("t_dd44", "A104 · Flaky", hours_ago=5, kind="transient")
        self.desk.store.put(f"alerts:{self.desk._now_dt(at(12, 0, day=3)).date().isoformat()}", 4)
        self.clock.t = at(7, 31)
        self.at_desk()
        self.assertEqual(self.desk.pump()["brief"], 1)
        brief = self.sent.to(DESK_KEY)[-1]
        self.assertIn("Morning brief", brief)
        waiting = brief.split("Waiting on you (2):\n")[1].split("\nFor you")[0].split("\nAlerts")[0]
        self.assertEqual(len(waiting.splitlines()), 2)
        self.assertIn("A101 · Pick a launch date (blocked", waiting)
        self.assertNotIn("A101 · A101", waiting)
        self.assertIn("A101 · Pick a launch date", brief)
        self.assertIn("t_bb22 · Untagged card", brief)   # no tag: the card id is the code
        self.assertNotIn("A103", waiting)                 # waiting on another card isn't the boss's
        self.assertNotIn("A104", waiting)                 # a flaky failure isn't either
        self.assertIn("4 interruption(s) yesterday, 0 so far today", brief)

    def test_default_brief_time_is_7_30_local(self):
        self.assertEqual(settings_mod.normalize({})["brief"], {"time": "07:30"})
        self.assertEqual(settings_mod.normalize({})["check_every_hours"], 3)

    def test_brief_not_before_its_time_and_once_a_day(self):
        self.settings(digest_times=[])
        self.start()
        self.at_desk()
        self.desk.ingest(Event(key="d1", category="card_done", title="A107 · Done"))
        self.clock.t = at(7, 29)
        self.assertEqual(self.desk.pump()["brief"], 0)
        self.clock.t = at(7, 30)
        self.assertEqual(self.desk.pump()["brief"], 1)
        self.clock.t = at(12, 0)
        self.at_desk()
        self.assertEqual(self.desk.pump()["brief"], 0)
        self.clock.t = at(7, 45, day=5)
        self.at_desk()
        self.desk.ingest(Event(key="d2", category="card_done", title="A108 · Done"))
        self.assertEqual(self.desk.pump()["brief"], 1)
        self.assertEqual(sum("Morning brief" in t for t, _ in self.sent.sent), 2)

    def test_install_never_replays_a_missed_brief(self):
        self.clock.t = at(9, 0)
        self.at_desk()
        self.assertEqual(self.desk.pump()["brief"], 0)  # 7:30 already passed before install
        self.assertEqual(self.sent.sent, [])

    def test_brief_carries_digest_items_and_quiet_hours_holds(self):
        self.settings(digest_times=[])
        self.start(day=4, hh=21)
        self.clock.t = at(23, 0)
        self.make_state_db(self.clock() - 3 * HOUR)  # asleep
        eid = self.desk.ingest(Event(key="k-night", category="deploy", title="Release R7 is waiting on you"))
        self.desk.apply_timeouts()
        self.clock.advance(11 * 60)
        self.desk.pump()
        self.assertEqual(self.desk.store.row(eid)["status"], "digest")  # held for quiet hours
        self.assertEqual(self.sent.sent, [])
        self.clock.t = at(7, 35, day=5)
        self.at_desk()
        self.desk.pump()
        self.assertIn("Release R7", self.sent.to(DESK_KEY)[-1])
        self.assertEqual(self.desk.store.row(eid)["status"], "delivered")

    def test_brief_goes_to_the_phone_when_away(self):
        self.settings(digest_times=[])
        self.start()
        self.desk.ingest(Event(key="d1", category="card_done", title="A109 · Done"))
        self.make_state_db(self.clock() - 5 * HOUR)
        self.clock.t = at(7, 31)
        self.desk.pump()
        self.assertIn("Morning brief", self.sent.to(PHONE_KEY)[-1])

    def test_brief_off(self):
        self.settings(brief="off", digest_times=["17:00"])
        self.start()
        self.at_desk()
        self.clock.t = at(7, 31)
        self.assertEqual(self.desk.pump()["brief"], 0)

    def test_brief_off_needs_a_digest_slot_so_nothing_is_stranded(self):
        with self.assertRaises(settings_mod.SettingsError) as cm:
            settings_mod.normalize({"brief": "off", "digest_times": []})
        self.assertIn("never reach you", str(cm.exception))
        with self.assertRaises(notify_cmd.SettingsError):
            notify_cmd.apply("brief off", settings_mod.normalize({}))

    def test_installed_before_brief_time_gets_todays_brief(self):
        self.settings(digest_times=[])
        self.start(hh=6)
        self.at_desk()
        self.desk.ingest(Event(key="d1", category="card_done", title="A105 · Done"))
        self.clock.t = at(7, 31)
        self.at_desk()
        self.assertEqual(self.desk.pump()["brief"], 1)

    def test_a_morning_with_nothing_to_say_sends_no_brief(self):
        self.settings(digest_times=[])
        self.start()
        self.at_desk()
        self.clock.t = at(7, 31)
        self.at_desk()
        self.assertEqual(self.desk.pump()["brief"], 0)
        self.assertEqual(self.sent.sent, [])
        self.clock.t = at(7, 31, day=5)  # and it doesn't pile up for the next day
        self.at_desk()
        self.assertEqual(self.desk.pump()["brief"], 0)

    def test_handled_money_before_a_brief_still_shows_once_in_the_next(self):
        self.settings(digest_times=[], always_interrupt={"categories": [], "people": []},
                      triage={"orchestrator_first": True, "timeout_minutes": 120})
        self.start(day=5, hh=6)
        self.at_desk()
        self.desk.ingest(Event(key="x", category="card_done", title="A106 · Filler"))
        self.clock.t = at(7, 0, day=5)
        eid = self.desk.ingest(Event(key="m7", category="money", title="Refund R-21 issued"))
        self.clock.t = at(7, 31, day=5)
        self.at_desk()
        self.desk.pump()  # today's brief went out before the refund was handled
        self.clock.t = at(8, 0, day=5)
        self.desk.triage(eid, "handled", "approved earlier")
        self.clock.t = at(7, 31, day=6)
        self.at_desk()
        self.desk.store.put(f"alerts:{self.desk._now_dt(at(12, 0, day=5)).date().isoformat()}", 1)
        self.desk.pump()
        self.assertIn("Refund R-21", self.sent.to(DESK_KEY)[-1])
        self.clock.t = at(7, 31, day=7)
        self.at_desk()
        self.desk.store.put(f"alerts:{self.desk._now_dt(at(12, 0, day=6)).date().isoformat()}", 1)
        self.desk.pump()
        self.assertNotIn("Refund R-21", self.sent.to(DESK_KEY)[-1])  # shown once

    def test_overflow_is_counted_never_silently_dropped(self):
        from front_desk import desk as desk_mod
        self.settings(digest_times=[], always_interrupt={"categories": [], "people": []})
        self.start()
        self.at_desk()
        for i in range(desk_mod.BRIEF_MAX + 2):
            self.desk.ingest(Event(key=f"d{i}", category="card_done", title=f"Done {i}"))
        for i in range(desk_mod.BRIEF_MAX + 1):
            e = self.desk.ingest(Event(key=f"m{i}", category="money", title=f"Refund {i}"))
            self.desk.triage(e, "handled")
        for i in range(desk_mod.WAITING_MAX + 1):
            self.block(f"t_w{i}", f"A{200 + i} · Pick {i}", hours_ago=1)
        self.clock.t = at(7, 31)
        self.at_desk()
        self.desk.pump()
        brief = self.sent.to(DESK_KEY)[-1]
        self.assertIn("and 2 more, in the next brief", brief)
        self.assertIn("and 1 more handled, in the next brief", brief)
        self.assertIn("and 1 more on the board", brief)
        self.assertEqual(len(self.desk.store.rows("status='digest'")), 2)
        self.assertEqual(len(self.desk.handled_floor()), 1)

    def test_a_card_waiting_on_you_is_listed_once(self):
        self.settings(digest_times=[], levels={"card_needs_input": "digest"})
        self.start()
        self.at_desk()
        self.desk.poll()
        self.block("t_once", "A110 · Pick one", hours_ago=0.2)
        self.clock.t = at(7, 31)
        self.at_desk()
        self.desk.pump()
        self.assertEqual(self.sent.to(DESK_KEY)[-1].count("A110"), 1)

    def test_money_the_orchestrator_handled_still_shows_in_the_brief(self):
        self.settings(digest_times=[], always_interrupt={"categories": [], "people": []})
        self.start()
        self.at_desk()
        eid = self.desk.ingest(Event(key="m1", category="money", title="Refund R-17 issued"))
        self.desk.triage(eid, "handled", "refund was already approved")
        self.clock.t = at(7, 31)
        self.at_desk()
        self.desk.pump()
        brief = self.sent.to(DESK_KEY)[-1]
        self.assertIn("Handled for you", brief)
        self.assertIn("Refund R-17", brief)


# --------------------------------------------------------------------------- regular checks
class RegularChecks(RhythmCase):
    def setUp(self):
        super().setUp()
        self.settings(responsibilities=["card waiting on me over 2 hours"], digest_times=[])
        self.at_desk()
        self.desk.pump()  # starts the check clock
        # Board intake already told the boss when each card blocked; these tests watch the check alone.
        self.desk.store.put("kanban_cursor", 10 ** 9)

    def test_check_stays_silent_when_nothing_needs_the_boss(self):
        self.clock.advance(3 * HOUR + 1)
        self.block("t_new1", "A201 · Fresh question", hours_ago=0.5)  # under the 2h watch at check time
        self.at_desk()
        out = self.desk.pump()
        self.assertEqual(out["watched"], 0)
        self.assertEqual(self.sent.sent, [])
        self.assertEqual(self.desk.store.rows(), [])

    def test_check_with_no_watches_is_silent(self):
        self.settings(digest_times=[])
        self.block("t_old1", "A202 · Old question", hours_ago=9)
        self.clock.advance(3 * HOUR + 1)
        self.at_desk()
        self.assertEqual(self.desk.pump()["watched"], 0)
        self.assertEqual(self.sent.sent, [])

    def test_checks_run_every_n_hours_not_sooner(self):
        self.block("t_late", "A203 · Late question", hours_ago=5)
        self.clock.advance(2 * HOUR)
        self.at_desk()
        self.assertEqual(self.desk.pump()["watched"], 0)  # not due yet
        self.clock.advance(HOUR + 1)
        self.at_desk()
        self.assertEqual(self.desk.pump()["watched"], 1)

    def test_breach_reaches_the_orchestrator_first_then_alerts_once(self):
        self.block("t_late", "A203 · Late question", hours_ago=5)
        self.clock.advance(3 * HOUR + 1)
        self.at_desk()
        self.desk.pump()
        woke = self.sent.to(DESK_KEY)
        self.assertEqual(len(woke), 1)
        self.assertIn("A203 has waited", woke[0])
        self.assertIn("triage", woke[0])
        self.clock.advance(3 * HOUR + 1)
        self.at_desk()
        self.assertEqual(self.desk.pump()["watched"], 0)  # same breach: never twice
        self.assertEqual(len([r for r in self.desk.store.rows() if r["source"] == "watch"]), 1)

    def test_a_new_block_on_the_same_card_is_a_new_breach(self):
        self.block("t_late", "A203 · Late question", hours_ago=5)
        self.clock.advance(3 * HOUR + 1)
        self.at_desk()
        self.desk.pump()
        self.block("t_late", "A203 · Late question", hours_ago=2.5)  # unblocked, then blocked again
        self.clock.advance(3 * HOUR + 1)
        self.at_desk()
        self.assertEqual(self.desk.pump()["watched"], 1)

    def test_checks_off(self):
        self.settings(responsibilities=["card waiting on me over 2 hours"], check_every_hours=0, digest_times=[])
        self.block("t_late", "A203 · Late question", hours_ago=5)
        self.clock.advance(10 * HOUR)
        self.at_desk()
        self.assertEqual(self.desk.pump()["watched"], 0)


# --------------------------------------------------------------------------- responsibilities
class Responsibilities(RhythmCase):
    PEOPLE = [{"name": "Dana Reyes", "match": ["dana"]}]

    def test_plain_words_parse_to_fixed_watches(self):
        ppl = settings_mod.normalize({"people": self.PEOPLE})["people"]
        cases = {
            "card waiting on me over 2 hours": ("card_waiting", 7200, "", ""),
            "tell me if a client card is blocked over 90 minutes": ("card_waiting", 5400, "client", ""),
            "nobody waits more than two days for a reply": ("reply_wait", 172800, "", ""),
            "Dana Reyes not answered in 1 day": ("reply_wait", 86400, "", "Dana Reyes"),
        }
        for text, (kind, secs, filt, person) in cases.items():
            with self.subTest(text=text):
                w = watches.parse(text, ppl)
                self.assertEqual((w["kind"], w["seconds"], w["filter"], w["person"]), (kind, secs, filt, person))

    def test_words_it_cannot_read_are_refused_with_a_reason(self):
        for text, why in [("keep an eye on things", "say how long"),
                          ("watch the weather over 2 hours", "I can watch cards"),
                          ("Pat not answered in 2 days", "people list")]:
            with self.subTest(text=text):
                with self.assertRaises(settings_mod.SettingsError) as cm:
                    settings_mod.normalize({"responsibilities": [text]})
                self.assertIn(why, str(cm.exception))

    def test_a_word_like_read_is_not_a_duration(self):
        with self.assertRaises(watches.WatchError):
            watches.parse("card waiting on me, read it", [])

    def test_words_that_end_in_a_unit_letter_are_not_durations(self):
        for text in ("tell me if a client card is blocked and stuck over 2 hours",
                     "card waiting on me and blocked over 2 hours", "tend to card blocked over 2 hours",
                     "ad card blocked over 2 hours"):
            with self.subTest(text=text):
                self.assertEqual(watches.parse(text, [])["seconds"], 7200)
        self.assertEqual(watches.parse("card blocked over 2h", [])["seconds"], 7200)
        self.assertEqual(watches.parse("card blocked over a day", [])["seconds"], 86400)
        with self.assertRaises(watches.WatchError) as cm:
            watches.parse("card blocked over 2 hours and 30 minutes", [])
        self.assertIn("one length of time", str(cm.exception))

    def test_status_words_before_card_are_not_a_filter(self):
        for text in ("a blocked card waiting on me over 2 hours", "any stuck card over 3 hours"):
            with self.subTest(text=text):
                self.assertEqual(watches.parse(text, [])["filter"], "")

    def test_person_names_match_whole_words(self):
        ppl = settings_mod.normalize({"people": [{"name": "Ed"}, {"name": "Dana Reyes"}]})["people"]
        self.assertEqual(watches.parse("Dana Reyes not answered in 1 day", ppl)["person"], "Dana Reyes")
        self.assertEqual(watches.parse("Ed not answered in 1 day", ppl)["person"], "Ed")

    def test_filter_only_matches_cards_with_the_word(self):
        self.settings(responsibilities=["tell me if a client card is blocked over 2 hours"], digest_times=[])
        self.block("t_x1", "A301 · Client logo pick", hours_ago=3)
        self.block("t_x2", "A302 · Internal refactor", hours_ago=3)
        self.assertEqual(self.desk.check_now(), 1)
        self.assertEqual([r["ref"] for r in self.desk.store.rows()], ["t_x1"])

    def test_named_person_not_answered_then_answered(self):
        self.settings(people=self.PEOPLE, responsibilities=["nobody waits more than two days for a reply"],
                      digest_times=[])
        self.desk.inbound(platform="telegram", sender="Dana", sender_id="5", chat_id="9", message_id="m1")
        self.clock.advance(1 * 86400)
        self.assertEqual(self.desk.check_now(), 0)  # one day: not yet
        self.clock.advance(1 * 86400 + 60)
        self.assertEqual(self.desk.check_now(), 1)
        self.assertEqual(self.desk.check_now(), 0)  # deduped
        row = [r for r in self.desk.store.rows() if r["source"] == "watch"][0]
        self.assertIn("Dana Reyes has waited 2 days", row["title"])
        self.assertEqual(self.desk.mark_answered("dana reyes"), "Dana Reyes")
        self.clock.advance(1)
        self.desk.inbound(platform="telegram", sender="Dana", sender_id="5", chat_id="9", message_id="m2")
        self.clock.advance(3 * 86400)
        self.assertEqual(self.desk.check_now(), 1)  # a new wait after the answer is a new breach

    def test_a_second_unanswered_message_after_a_breach_is_a_new_wait(self):
        self.settings(people=self.PEOPLE, responsibilities=["nobody waits more than two days for a reply"],
                      digest_times=[])
        self.desk.inbound(platform="telegram", sender="Dana", sender_id="5", chat_id="9", message_id="m1")
        self.clock.advance(2 * 86400 + 60)
        self.assertEqual(self.desk.check_now(), 1)
        self.clock.advance(86400)
        self.desk.inbound(platform="telegram", sender="Dana", sender_id="5", chat_id="9", message_id="m2")
        self.clock.advance(1 * 86400)
        self.assertEqual(self.desk.check_now(), 0)  # m2 has waited one day
        self.clock.advance(1 * 86400 + 60)
        self.assertEqual(self.desk.check_now(), 1)  # no answer seen: m2 is reported too
        self.assertEqual(self.desk.check_now(), 0)

    def test_watch_card_is_made_once_and_noted_on_each_breach(self):
        fb = FakeBoard()
        self.desk.board = fb
        self.settings(responsibilities=[{"text": "card waiting on me over 2 hours", "card": True}], digest_times=[])
        self.block("t_y1", "A401 · Pick", hours_ago=3)
        self.desk.check_now()
        self.block("t_y2", "A402 · Pick again", hours_ago=4)
        self.desk.check_now()
        self.assertEqual(fb.cards, ["card waiting on me over 2 hours"])
        self.assertEqual([c for c, _ in fb.comments], ["t_watch1", "t_watch1"])
        self.assertEqual(len(fb.comments), 2)

    def test_no_card_unless_asked(self):
        fb = FakeBoard()
        self.desk.board = fb
        self.settings(responsibilities=["card waiting on me over 2 hours"], digest_times=[])
        self.block("t_y1", "A401 · Pick", hours_ago=3)
        self.desk.check_now()
        self.assertEqual((fb.cards, fb.comments), ([], []))

    def test_the_desks_own_watch_cards_are_never_intake(self):
        self.desk.poll()
        self.block("t_w", "Watch: card waiting on me over 2 hours", hours_ago=9, by=sources.DESK_AUTHOR)
        self.assertEqual(self.desk.poll(), 0)
        self.assertEqual(sources.blocked_on_boss(self.board), [])

    def test_watch_level_digest_goes_to_the_brief(self):
        self.settings(responsibilities=[{"text": "card waiting on me over 2 hours", "level": "digest"}],
                      digest_times=[])
        self.block("t_y1", "A401 · Pick", hours_ago=3)
        self.desk.check_now()
        self.assertEqual(self.desk.store.rows()[0]["level"], "digest")

    def test_notify_adds_and_removes_a_watch(self):
        s = base_settings()
        s2, msg = notify_cmd.apply("watch card waiting on me over 2 hours", s)
        self.assertIn("Undo: /notify stop watching 1", msg)
        self.assertEqual(s2["responsibilities"][0]["kind"], "card_waiting")
        s3, msg = notify_cmd.apply("stop watching 1", s2)
        self.assertEqual(s3["responsibilities"], [])
        self.assertIn("Undo: /notify watch card waiting on me over 2 hours", msg)

    def test_watches_round_trip_through_the_file_as_words(self):
        p = self.tmp / "settings.yaml"
        s = settings_mod.normalize({"responsibilities": ["card waiting on me over 2 hours",
                                                         {"text": "nobody waits more than 2 days for a reply",
                                                          "card": True}]})
        settings_mod.save(p, s)
        text = p.read_text()
        self.assertIn("card waiting on me over 2 hours", text)
        self.assertNotIn("seconds", text)
        again = settings_mod.load(p)
        self.assertEqual([w["id"] for w in again["responsibilities"]], [w["id"] for w in s["responsibilities"]])
        self.assertTrue(again["responsibilities"][1]["card"])

    def test_card_code(self):
        self.assertEqual(watches.card_code("t_1", "A101 · Front desk"), "A101")
        self.assertEqual(watches.card_code("t_1", "plain title"), "t_1")


# --------------------------------------------------------------------------- corrections
class Corrections(RhythmCase):
    def alert(self, key="c1", category="card_needs_input", title="A501 · Vendor sent new terms", person=""):
        self.at_desk()
        eid = self.desk.ingest(Event(key=key, category=category, title=title, person=person))
        self.desk.triage(eid, "tell_boss", in_live_session=True)
        return eid

    def test_phrases_and_reason(self):
        cases = {
            "that could have waited, vendor updates only matter when a date or price changes":
                "vendor updates only matter when a date or price changes",
            "This could have waited until the morning brief. Vendor news is low priority": "Vendor news is low priority",
            "that could've waited": "",
            "that should have been in the brief": "",
            "it wasn't urgent: just an FYI": "just an FYI",
        }
        for text, why in cases.items():
            with self.subTest(text=text):
                self.assertEqual(notify_cmd.parse_correction(text), why)
        for text in ("stop telling me about finished cards", "that was great", "waited"):
            self.assertIsNone(notify_cmd.parse_correction(text))

    def test_proposal_names_the_alert_then_yes_saves_with_an_undo_line(self):
        eid = self.alert()
        rule, msg = notify_cmd.propose_rule(self.desk.last_alert(), self.cfg, "vendor news can wait", "2026-03-04")
        self.assertIn(f"alert #{eid}", msg)
        self.assertIn("/notify yes", msg)
        new, saved = notify_cmd.save_rule(rule, self.cfg)
        self.assertIn("Undo: /notify forget rule 1", saved)
        self.assertIn('Reason: "vendor news can wait"', saved)
        self.assertEqual(new["rules"][0]["reason"], "vendor news can wait")
        self.cfg = new
        eid2 = self.desk.ingest(Event(key="c2", category="card_needs_input", title="A502 · Another vendor note"))
        row = self.desk.store.row(eid2)
        self.assertEqual(row["level"], "digest")
        self.assertIn("boss's rule 1: vendor news can wait", row["note"])
        back, forgot = notify_cmd.forget_rule(1, new)
        self.assertEqual(back["rules"], [])
        self.assertIn("Forgot rule 1", forgot)

    def test_rule_for_a_person_only_matches_that_person(self):
        self.cfg = base_settings(people=[{"name": "Dana Reyes"}], rules=[
            {"category": "inbound_person", "person": "Dana Reyes", "level": "digest", "reason": "r"}])
        self.assertIsNotNone(policy.rule_for(Event(key="a", category="inbound_person", title="x", person="Dana Reyes"), self.cfg))
        self.assertIsNone(policy.rule_for(Event(key="b", category="inbound_person", title="x", person="Lee Park"), self.cfg))

    def test_no_recent_alert_changes_nothing(self):
        rule, msg = notify_cmd.propose_rule(None, self.cfg, "why", "2026-03-04")
        self.assertIsNone(rule)
        self.assertIn("nothing changed", msg)

    def test_always_interrupt_cannot_be_held_back_by_a_rule(self):
        self.at_desk()
        eid = self.desk.ingest(Event(key="m", category="money", title="Payout P-3 failed"))
        self.desk.deliver_due()
        self.assertEqual(self.desk.last_alert()["id"], eid)
        rule, msg = notify_cmd.propose_rule(self.desk.last_alert(), self.cfg, "x", "2026-03-04")
        self.assertIsNone(rule)
        self.assertIn("stop always interrupting for money", msg)

    def test_an_old_alert_is_not_a_correction_target(self):
        self.alert()
        self.assertIsNotNone(self.desk.last_alert())
        self.clock.advance(86400 + 1)
        self.assertIsNone(self.desk.last_alert())

    def test_a_digest_delivery_is_not_an_alert(self):
        self.at_desk()
        eid = self.desk.ingest(Event(key="d", category="card_done", title="Done"))
        self.desk.triage(eid, "tell_boss")
        self.desk.store.put("last_digest", self.clock() - 1)
        self.clock.t = at(17, 1)
        self.at_desk()
        self.desk.pump()
        self.assertEqual(self.desk.store.row(eid)["status"], "delivered")
        self.assertIsNone(self.desk.last_alert())
        self.assertEqual(self.desk.alert_count(), 0)


# --------------------------------------------------------------------------- locked floors
class LockedFloors(unittest.TestCase):
    def test_settings_refuse_never_or_log_for_money_and_approval(self):
        for cat in ("money", "approval"):
            for lvl in ("never", "log"):
                with self.subTest(cat=cat, lvl=lvl):
                    with self.assertRaises(settings_mod.SettingsError) as cm:
                        settings_mod.normalize({"levels": {cat: lvl}})
                    self.assertIn("always reach you", str(cm.exception))
        settings_mod.normalize({"levels": {"money": "digest", "approval": "digest"}})  # lowest allowed

    def test_notify_path_refuses_and_says_why(self):
        s = settings_mod.normalize({})
        for text in ("never show money", "stop telling me about approvals", "stop telling me about payments"):
            with self.subTest(text=text):
                with self.assertRaises(notify_cmd.SettingsError) as cm:
                    notify_cmd.apply(text, s)
                self.assertIn("always reach you", str(cm.exception))

    def test_a_rule_cannot_go_under_the_floor(self):
        with self.assertRaises(settings_mod.SettingsError):
            settings_mod.normalize({"rules": [{"category": "money", "level": "never"}]})

    def test_policy_clamps_even_a_hand_built_dict(self):
        s = settings_mod.normalize({"always_interrupt": {"categories": [], "people": []}})
        s["levels"]["money"] = "never"  # bypassing the validator on purpose
        self.assertEqual(policy.level_for(Event(key="x", category="money", title="t"), s), "digest")

    def test_yaml_load_path_refuses_a_floor_breach(self):
        import tempfile
        from pathlib import Path
        p = Path(tempfile.mkdtemp()) / "settings.yaml"
        p.write_text(json.dumps({"levels": {"approval": "never"}}))  # JSON is valid YAML
        with self.assertRaises(settings_mod.SettingsError):
            settings_mod.load(p)

    def test_approval_keyword_refines_a_blocked_card(self):
        s = settings_mod.normalize({})
        self.assertEqual(policy.classify("card_needs_input", "Draft for approval: reply to a lead", s), "approval")


class AlertCounter(RhythmCase):
    def test_each_interrupt_counts_once_per_day(self):
        self.settings(digest_times=[])
        self.make_state_db(self.clock() - 5 * HOUR)  # away: phone alerts
        for i in range(3):
            self.desk.ingest(Event(key=f"m{i}", category="money", title=f"Payout {i}"))
        self.desk.deliver_due()
        self.desk.deliver_due()
        self.assertEqual(self.desk.alert_count(), 3)
        self.clock.advance(24 * HOUR)
        self.assertEqual((self.desk.alert_count(0), self.desk.alert_count(1)), (0, 3))
        self.assertEqual(self.desk.status_report()["alerts_yesterday"], 3)


if __name__ == "__main__":
    unittest.main()
