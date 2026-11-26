import json
import sqlite3
import unittest

from helpers import DESK_KEY, DeskCase
from front_desk import sources


def make_board(path, events):
    con = sqlite3.connect(path)
    con.execute("CREATE TABLE IF NOT EXISTS tasks (id TEXT PRIMARY KEY, title TEXT, assignee TEXT)")
    con.execute("CREATE TABLE IF NOT EXISTS task_events (id INTEGER PRIMARY KEY AUTOINCREMENT, task_id TEXT,"
                " run_id INTEGER, kind TEXT, payload TEXT, created_at INTEGER)")
    for task_id, title, kind, payload in events:
        con.execute("INSERT OR IGNORE INTO tasks VALUES (?,?,?)", (task_id, title, "worker-a"))
        con.execute("INSERT INTO task_events(task_id, run_id, kind, payload, created_at) VALUES (?,?,?,?,0)",
                    (task_id, 1, kind, json.dumps(payload)))
    con.commit()
    con.close()


class KanbanIntake(DeskCase):
    def setUp(self):
        super().setUp()
        self.board = self.tmp / "kanban.db"
        make_board(self.board, [("t_old", "Old card", "blocked", {"kind": "needs_input", "reason": "old"})])
        self.desk.kanban_db = self.board

    def test_first_poll_never_replays_history(self):
        self.assertEqual(self.desk.poll(), 0)
        self.assertEqual(self.desk.store.rows(), [])

    def test_any_lane_reaches_the_desk(self):
        self.desk.poll()
        make_board(self.board, [
            ("t_a", "Pick a cover", "blocked", {"kind": "needs_input", "reason": "which of two covers?"}),
            ("t_b", "Responder escalation", "blocked", {"reason": "initial_status", "status": "blocked"}),
            ("t_c", "Wait for parent", "blocked", {"kind": "dependency"}),
            ("t_d", "Build thing", "gave_up", {"error": "pid not alive"}),
            ("t_e", "Retry me", "crashed", {"retry_status": "ready"}),
            ("t_f", "Ship notes", "completed", {"summary": "done"}),
            ("t_g", "Card about an invoice", "blocked", {"kind": "needs_input", "reason": "refund amount?"}),
        ])
        self.assertEqual(self.desk.poll(), 5)
        cats = {r["ref"]: r["category"] for r in self.desk.store.rows()}
        self.assertEqual(cats, {"t_a": "card_needs_input", "t_b": "client_escalation", "t_d": "card_failed",
                                "t_f": "card_done", "t_g": "money"})

    def test_board_poll_is_deduped(self):
        self.desk.poll()
        make_board(self.board, [("t_a", "Pick", "blocked", {"kind": "needs_input"})])
        self.desk.poll()
        self.desk.store.put("kanban_cursor", 0)  # a cursor reset re-reads the same events
        self.desk.poll()
        self.assertEqual(len(self.desk.store.rows("ref='t_a'")), 1)

    def test_board_item_is_routed_to_the_live_session(self):
        self.at_desk()
        self.desk.pump()
        make_board(self.board, [("t_a", "Pick", "blocked", {"kind": "needs_input"})])
        self.desk.pump()
        self.assertEqual(len(self.sent.to(DESK_KEY)), 1)

    def test_board_is_opened_read_only(self):
        self.desk.poll()
        before = self.board.read_bytes()
        make_board(self.board, [("t_a", "Pick", "blocked", {"kind": "needs_input"})])
        after_write = self.board.read_bytes()
        self.desk.poll()
        self.assertNotEqual(before, after_write)
        self.assertEqual(after_write, self.board.read_bytes())


class CronIntake(DeskCase):
    def write_jobs(self, jobs):
        (self.tmp / "jobs.json").write_text(json.dumps({"jobs": jobs}))

    def test_cron_errors_once_per_run(self):
        self.desk.cron_jobs = self.tmp / "jobs.json"
        self.write_jobs([{"id": "j1", "name": "Nightly export", "last_status": "ok", "last_run_at": "1"}])
        self.assertEqual(self.desk.poll(), 0)
        self.write_jobs([{"id": "j1", "name": "Nightly export", "last_status": "error", "last_run_at": "2",
                          "failure_streak": 1, "last_error": "timeout"}])
        self.assertEqual(self.desk.poll(), 1)
        self.assertEqual(self.desk.poll(), 0)
        self.write_jobs([{"id": "j1", "name": "Nightly export", "last_status": "ok", "last_run_at": "3"}])
        self.assertEqual(self.desk.poll(), 0)
        self.assertEqual(self.desk.store.rows()[0]["category"], "cron_error")

    def test_bad_jobs_file_is_ignored(self):
        self.desk.cron_jobs = self.tmp / "jobs.json"
        (self.tmp / "jobs.json").write_text("{not json")
        self.assertEqual(sources.poll_cron(self.desk.cron_jobs, None, self.cfg)[0], [])


if __name__ == "__main__":
    unittest.main()
