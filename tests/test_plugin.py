"""The Hermes wiring, against stubbed hosts: v0.23.3 (no presence API) and v0.23.4 (session_viewers)."""
import importlib.util
import json
import os
import sys
import tempfile
import types
import unittest
from pathlib import Path

import helpers  # noqa: F401
from helpers import HAVE_YAML, NO_YAML

ROOT = Path(__file__).resolve().parents[1]
_n = 0


class FakeCtx:
    def __init__(self, surface="desktop", viewers=None):
        self.hooks, self.tools, self.commands, self.cli = {}, {}, {}, {}
        self.injected = []
        self._surface = surface
        if viewers is not None:
            self.session_viewers = lambda: viewers

    def register_hook(self, name, cb, **kw):
        self.hooks[name] = cb

    def register_tool(self, name, toolset, schema, handler, **kw):
        self.tools[name] = handler

    def register_command(self, name, handler, **kw):
        self.commands[name] = handler

    def register_cli_command(self, name, help, setup_fn, handler_fn=None, **kw):
        self.cli[name] = (setup_fn, handler_fn)

    def get_config(self, key, default=None):
        return default

    def inject_message(self, content, role="user", *, session_key=None, origin=None):
        self.injected.append((content, session_key, origin))
        return True

    def session_surface(self):
        return self._surface


def load_plugin(ctx, *, with_viewers_module=False, session_key="agent:main:local:desk"):
    """Import the repo as a plugin package with a stub hermes_cli, then register(ctx)."""
    global _n
    _n += 1
    home = Path(tempfile.mkdtemp(prefix="fd-home-"))
    os.environ["HERMES_HOME"] = str(home)
    ps = types.ModuleType("hermes_cli.plugin_session")
    ps.current_key = session_key
    ps.session_key = lambda: ps.current_key
    if with_viewers_module:
        ps.session_viewers = lambda: [{"idle_seconds": 5}]
        ps.turn_origin = lambda: None
    hc = types.ModuleType("hermes_cli")
    hc.plugin_session = ps
    sys.modules["hermes_cli"] = hc
    sys.modules["hermes_cli.plugin_session"] = ps
    name = f"fd_plugin_{_n}"
    spec = importlib.util.spec_from_file_location(name, ROOT / "__init__.py", submodule_search_locations=[str(ROOT)])
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    mod.register(ctx)
    return mod, mod._PLUGIN, ps, home


class Wiring(unittest.TestCase):
    def tearDown(self):
        os.environ.pop("HERMES_KANBAN_TASK", None)
        for k in ("hermes_cli", "hermes_cli.plugin_session"):
            sys.modules.pop(k, None)

    @unittest.skipUnless(HAVE_YAML, NO_YAML)
    def test_registers_what_the_manifest_declares(self):
        import yaml
        ctx = FakeCtx()
        load_plugin(ctx)
        man = yaml.safe_load((ROOT / "plugin.yaml").read_text())
        self.assertEqual(sorted(ctx.hooks), sorted(man["provides_hooks"]))
        self.assertEqual(sorted(ctx.tools), sorted(man["provides_tools"]))
        self.assertIn("notify", ctx.commands)
        self.assertIn("front-desk", ctx.cli)

    def test_v0233_runs_degraded(self):
        _, p, _, _ = load_plugin(FakeCtx())
        self.assertFalse(p.desk.full_presence)
        self.assertIsNone(p._viewers())

    def test_v0234_uses_session_viewers(self):
        _, p, _, _ = load_plugin(FakeCtx(viewers=[{"idle_seconds": 3}]))
        self.assertTrue(p.desk.full_presence)
        self.assertEqual(p._viewers(), [{"idle_seconds": 3}])
        _, p2, _, _ = load_plugin(FakeCtx(), with_viewers_module=True)  # module-level form only
        self.assertTrue(p2.desk.full_presence)

    def test_feature_detected_never_version_sniffed(self):
        code = "\n".join(f.read_text() for f in [ROOT / "__init__.py", *(ROOT / "front_desk").glob("*.py")])
        for needle in ("0.23", "__version__", "hermes --version", "packaging.version"):
            self.assertNotIn(needle, code)

    def test_escalation_reaches_the_live_session_context(self):
        ctx = FakeCtx()
        _, p, ps, _ = load_plugin(ctx)
        self.assertIsNone(ctx.hooks["pre_llm_call"](user_message="hi", platform="desktop"))
        out = json.loads(ctx.tools["front_desk_escalate"]({"category": "card_needs_input",
                                                            "title": "Card t_9 needs a pick"}))
        self.assertTrue(out["ok"])
        dup = json.loads(ctx.tools["front_desk_escalate"]({"category": "card_needs_input",
                                                            "title": "Card t_9 needs a pick"}))
        self.assertTrue(dup["duplicate"])
        res = ctx.hooks["pre_llm_call"](user_message="next", platform="desktop")
        self.assertIn(f"#{out['id']}", res["context"])

    def test_workers_get_no_context_and_cannot_triage(self):
        ctx = FakeCtx()
        _, p, ps, _ = load_plugin(ctx)
        ctx.hooks["pre_llm_call"](user_message="hi", platform="desktop")
        eid = json.loads(ctx.tools["front_desk_escalate"]({"category": "money", "title": "x"}))["id"]
        os.environ["HERMES_KANBAN_TASK"] = "t_worker"
        ps.current_key = "agent:main:kanban:t_worker"
        self.assertIsNone(ctx.hooks["pre_llm_call"](user_message="work", platform="kanban"))
        self.assertEqual(p.desk.live_session()["session_key"], "agent:main:local:desk")
        self.assertFalse(json.loads(ctx.tools["front_desk_triage"]({"event_id": eid, "decision": "handled"}))["ok"])

    def test_triage_in_the_live_session_tells_the_user_there(self):
        ctx = FakeCtx(viewers=[{"idle_seconds": 2}])
        _, p, _, _ = load_plugin(ctx)
        ctx.hooks["pre_llm_call"](user_message="hi", platform="desktop")
        eid = json.loads(ctx.tools["front_desk_escalate"]({"category": "card_needs_input", "title": "pick one"}))["id"]
        res = json.loads(ctx.tools["front_desk_triage"]({"event_id": eid, "decision": "tell_boss", "note": "n"}))
        self.assertEqual((res["status"], res["channel"]), ("delivered", "in_session"))

    def test_notify_command_edits_the_file(self):
        ctx = FakeCtx()
        _, p, _, _ = load_plugin(ctx)
        msg = ctx.commands["notify"]("stop telling me about finished cards")
        self.assertIn("Undo", msg)
        self.assertEqual(p.settings()["levels"]["card_done"], "log")
        self.assertIn("finished cards: log it", ctx.commands["notify"](""))
        self.assertIn("didn't change anything", ctx.commands["notify"]("make it nicer"))

    def test_gateway_observer_never_skips_and_stores_no_text(self):
        ctx = FakeCtx()
        _, p, _, _ = load_plugin(ctx)
        p.settings_path.write_text(json.dumps({"people": [{"name": "Dana Reyes", "match": ["dana"]}]}))  # JSON is YAML
        src = types.SimpleNamespace(platform=types.SimpleNamespace(value="telegram"), user_name="Dana",
                                    user_id="5", chat_id="9")
        ev = types.SimpleNamespace(source=src, message_id="77", text="SECRET-BODY-TEXT")
        import time as _t
        p._last_pump = _t.time()  # hold the pump thread back so the hook path is observed alone
        self.assertIsNone(ctx.hooks["pre_gateway_dispatch"](event=ev, gateway=None, session_store=None))
        self.assertEqual(p.desk.store.rows(), [])  # the hook itself writes nothing
        self.assertEqual(p.drain_inbound(), 1)
        rows = p.desk.store.rows()
        self.assertEqual(len(rows), 1)
        self.assertNotIn("SECRET-BODY-TEXT", json.dumps([dict(r) for r in rows]))

    def test_redelivered_message_without_an_id_dedupes(self):
        ctx = FakeCtx()
        _, p, _, _ = load_plugin(ctx)
        p.settings_path.write_text(json.dumps({"people": [{"name": "Dana Reyes", "match": ["dana"]}]}))  # JSON is YAML
        import time as _t
        p._last_pump = _t.time()
        src = types.SimpleNamespace(platform="telegram", user_name="Dana", user_id="5", chat_id="9")
        ev = types.SimpleNamespace(source=src, message_id=None, text="x")
        ctx.hooks["pre_gateway_dispatch"](event=ev)
        ctx.hooks["pre_gateway_dispatch"](event=ev)
        p.drain_inbound()
        self.assertEqual(len(p.desk.store.rows()), 1)

    def test_correction_with_a_reason_is_offered_then_saved_on_yes(self):
        ctx = FakeCtx(viewers=[{"idle_seconds": 2}])
        _, p, _, _ = load_plugin(ctx)
        notify = ctx.commands["notify"]
        self.assertIn("no rule waiting", notify("yes"))
        self.assertIn("don't have a recent alert", notify("that could have waited, x"))
        ctx.hooks["pre_llm_call"](user_message="hi", platform="desktop")
        eid = json.loads(ctx.tools["front_desk_escalate"]({"category": "deploy", "title": "Release R2 went out"}))["id"]
        json.loads(ctx.tools["front_desk_triage"]({"event_id": eid, "decision": "tell_boss"}))
        offer = notify("that could have waited, releases only matter to me when one fails")
        self.assertIn(f"alert #{eid}", offer)
        self.assertEqual(p.settings()["rules"], [])  # nothing saved before the yes
        saved = notify("yes")
        self.assertIn("Undo: /notify forget rule 1", saved)
        self.assertEqual(p.settings()["rules"][0]["reason"], "releases only matter to me when one fails")
        self.assertIn("Saved rules", notify(""))
        self.assertIn("no rule waiting", notify("yes"))  # a yes is used once
        self.assertIn("Forgot rule 1", notify("forget rule 1"))
        self.assertEqual(p.settings()["rules"], [])

    def test_offer_expires(self):
        ctx = FakeCtx(viewers=[{"idle_seconds": 2}])
        mod, p, _, _ = load_plugin(ctx)
        ctx.hooks["pre_llm_call"](user_message="hi", platform="desktop")
        eid = json.loads(ctx.tools["front_desk_escalate"]({"category": "deploy", "title": "Release R3"}))["id"]
        ctx.tools["front_desk_triage"]({"event_id": eid, "decision": "tell_boss"})
        ctx.commands["notify"]("that could have waited")
        pending = p.desk.store.get("pending_rule")
        pending["ts"] -= mod.PENDING_RULE_S + 1
        p.desk.store.put("pending_rule", pending)
        self.assertIn("no rule waiting", ctx.commands["notify"]("yes"))
        self.assertEqual(p.settings()["rules"], [])

    def test_floor_refusal_says_why_and_changes_nothing(self):
        ctx = FakeCtx()
        _, p, _, _ = load_plugin(ctx)
        msg = ctx.commands["notify"]("never show money")
        self.assertIn("didn't change anything", msg)
        self.assertIn("always reach you", msg)
        self.assertFalse(p.settings_path.exists())

    def test_broken_floor_in_the_file_keeps_the_last_good_settings(self):
        ctx = FakeCtx()
        _, p, _, _ = load_plugin(ctx)
        p.settings_path.write_text(json.dumps({"levels": {"money": "never"}}))
        self.assertEqual(p.settings()["levels"]["money"], "interrupt")
        self.assertIn("always reach you", ctx.commands["notify"](""))

    def test_answered_and_watch_phrases(self):
        ctx = FakeCtx()
        _, p, _, _ = load_plugin(ctx)
        p.settings_path.write_text(json.dumps({"people": [{"name": "Dana Reyes", "match": ["dana"]}]}))
        self.assertIn("Marked Dana Reyes as answered", ctx.commands["notify"]("answered Dana Reyes"))
        self.assertIn("isn't in your people list", ctx.commands["notify"]("answered Lee"))
        self.assertIn("Undo: /notify stop watching 1",
                      ctx.commands["notify"]("watch Dana Reyes not answered in 2 days"))
        self.assertEqual(p.settings()["responsibilities"][0]["person"], "Dana Reyes")

    def test_no_kanban_api_means_no_watch_cards(self):
        _, p, _, _ = load_plugin(FakeCtx())
        self.assertIsNone(p.desk.board)

    def test_delivery_only_targets_the_given_session_or_phone(self):
        ctx = FakeCtx()
        _, p, _, _ = load_plugin(ctx)
        self.assertTrue(p.deliver("x", {"session_key": "k"}))
        self.assertFalse(p.deliver("x", {}))
        self.assertEqual(ctx.injected, [("x", "k", None)])


if __name__ == "__main__":
    unittest.main()
