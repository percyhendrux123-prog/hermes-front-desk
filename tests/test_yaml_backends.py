"""Which YAML parser the settings file uses.

Hermes itself depends on ruamel.yaml, not PyYAML, so an installed copy usually has no PyYAML.
Hand-written YAML settings must still load there, through ruamel.yaml.
"""
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import helpers  # noqa: F401  (sys.path)
from front_desk import settings as settings_mod

try:
    import ruamel.yaml  # noqa: F401
    HAVE_RUAMEL = True
except ImportError:
    HAVE_RUAMEL = False
NO_RUAMEL = ("ruamel.yaml not installed; run: uv run --no-project --python 3.12 --with pyyaml "
             "--with ruamel.yaml==0.18.16 python -m unittest discover -s tests")

HAND_WRITTEN = """\
# written by hand, the way the README shows it
brief: off
digest_times: ["12:00"]
channels:
  away: phone
levels:
  card_done: log
people:
  - name: Dana Reyes
    match: [dana]
responsibilities:
  - text: tell me if a card waits on me for over 2 hours
"""


def _tmpdir() -> Path:
    return Path(tempfile.mkdtemp(dir=os.environ.get("TMPDIR") or tempfile.gettempdir()))


class NoPyYAML:
    """Make `import yaml` fail, as on a Hermes install; optionally hide ruamel.yaml too."""

    def __init__(self, hide_ruamel: bool = False):
        hidden = {"yaml": None}
        if hide_ruamel:
            hidden.update({"ruamel": None, "ruamel.yaml": None})
        self._patch = mock.patch.dict(sys.modules, hidden)

    def __enter__(self):
        self._patch.start()

    def __exit__(self, *exc):
        self._patch.stop()


@unittest.skipUnless(HAVE_RUAMEL, NO_RUAMEL)
class RuamelWhenNoPyYAML(unittest.TestCase):
    def test_backend_is_ruamel_without_pyyaml(self):
        with NoPyYAML():
            self.assertIsInstance(settings_mod._yaml(), settings_mod._RuamelSafe)

    def test_hand_written_yaml_loads_without_pyyaml(self):
        p = _tmpdir() / "settings.yaml"
        p.write_text(HAND_WRITTEN, encoding="utf-8")
        with NoPyYAML():
            s = settings_mod.load(p)
        self.assertIsNone(s["brief"])                 # YAML 1.1: bare `off` is off
        self.assertEqual(s["digest_times"], ["12:00"])
        self.assertEqual(s["levels"]["card_done"], "log")
        self.assertEqual(s["people"][0]["name"], "Dana Reyes")

    def test_save_then_load_round_trips_as_yaml_without_pyyaml(self):
        p = _tmpdir() / "settings.yaml"
        p.write_text(HAND_WRITTEN, encoding="utf-8")
        with NoPyYAML():
            s = settings_mod.load(p)
            settings_mod.save(p, s)
            text = p.read_text(encoding="utf-8")
            again = settings_mod.load(p)
        self.assertFalse(text.split("\n", 3)[-1].lstrip().startswith("{"))  # YAML, not JSON
        self.assertIn("brief: 'off'", text)           # quoted, so any 1.1 or 1.2 reader agrees
        self.assertEqual(again, s)

    def test_broken_yaml_still_fails_loud_without_pyyaml(self):
        p = _tmpdir() / "settings.yaml"
        p.write_text("levels: [unclosed\n", encoding="utf-8")
        with NoPyYAML():
            with self.assertRaises(Exception):
                settings_mod.load(p)


class JsonOnlyWithNoParser(unittest.TestCase):
    def test_json_form_loads_with_no_yaml_parser(self):
        p = _tmpdir() / "settings.yaml"
        p.write_text('{"levels": {"card_done": "log"}}\n', encoding="utf-8")
        with NoPyYAML(hide_ruamel=True):
            self.assertIsNone(settings_mod._yaml())
            s = settings_mod.load(p)
        self.assertEqual(s["levels"]["card_done"], "log")

    def test_hand_written_yaml_fails_loud_naming_both_parsers(self):
        p = _tmpdir() / "settings.yaml"
        p.write_text(HAND_WRITTEN, encoding="utf-8")
        with NoPyYAML(hide_ruamel=True):
            with self.assertRaises(settings_mod.SettingsError) as cm:
                settings_mod.load(p)
        self.assertIn("PyYAML or ruamel.yaml", str(cm.exception))


if __name__ == "__main__":
    unittest.main()
