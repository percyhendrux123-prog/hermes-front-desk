import unittest

import helpers  # noqa: F401  (sys.path)
from front_desk import notify_cmd, settings as settings_mod


def s():
    return settings_mod.normalize({})


class NotifyPlainWords(unittest.TestCase):
    def test_stop_telling_me_about_finished_cards(self):
        new, msg = notify_cmd.apply("stop telling me about finished cards", s())
        self.assertEqual(new["levels"]["card_done"], "log")
        self.assertIn("Undo", msg)

    def test_undo_phrase_round_trips(self):
        new, msg = notify_cmd.apply("stop telling me about finished cards", s())
        undo = msg.split("Undo: /notify ", 1)[1]
        back, _ = notify_cmd.apply(undo, new)
        self.assertEqual(back["levels"]["card_done"], "digest")

    def test_each_level(self):
        cases = {
            "interrupt me for failed cards": ("card_failed", "interrupt"),
            "put cron errors in the digest": ("cron_error", "digest"),
            "never show scheduled jobs": ("cron_error", "never"),
            "don't tell me about deploys": ("deploy", "log"),
        }
        for text, (cat, lvl) in cases.items():
            with self.subTest(text=text):
                self.assertEqual(notify_cmd.apply(text, s())[0]["levels"][cat], lvl)

    def test_always_interrupt_for_a_person_and_a_category(self):
        new, _ = notify_cmd.apply("always interrupt for Dana Reyes", s())
        self.assertIn("Dana Reyes", new["always_interrupt"]["people"])
        self.assertEqual(new["people"][0]["match"], ["dana reyes"])
        new, _ = notify_cmd.apply("always interrupt me for deploys", new)
        self.assertIn("deploy", new["always_interrupt"]["categories"])
        new, _ = notify_cmd.apply("stop always interrupting for Dana Reyes", new)
        self.assertNotIn("Dana Reyes", new["always_interrupt"]["people"])

    def test_quiet_hours_and_digest_times(self):
        new, _ = notify_cmd.apply("quiet hours 10pm to 6:30am", s())
        self.assertEqual(new["quiet_hours"], {"start": "22:00", "end": "06:30"})
        new, _ = notify_cmd.apply("quiet hours off", new)
        self.assertIsNone(new["quiet_hours"])
        new, _ = notify_cmd.apply("digest at 8am and 4pm", new)
        self.assertEqual(new["digest_times"], ["08:00", "16:00"])

    def test_unclear_words_change_nothing(self):
        for text in ["", "make it better", "quiet hours 25pm to 7am"]:
            with self.subTest(text=text):
                with self.assertRaises((notify_cmd.Unparsed, settings_mod.SettingsError)):
                    notify_cmd.apply(text, s())


class SettingsFile(unittest.TestCase):
    def test_bad_values_are_refused(self):
        for raw in [{"levels": {"money": "loud"}}, {"channels": {"interrupt": {"away": "pager"}}},
                    {"quiet_hours": {"start": "9", "end": "7"}}, {"levels": {"bogus": "log"}}]:
            with self.subTest(raw=raw):
                with self.assertRaises(settings_mod.SettingsError):
                    settings_mod.normalize(raw)

    def test_save_load_round_trip_keeps_a_backup(self):
        import tempfile
        from pathlib import Path
        p = Path(tempfile.mkdtemp()) / "settings.yaml"
        a = s()
        settings_mod.save(p, a)
        b, _ = notify_cmd.apply("stop telling me about finished cards", a)
        settings_mod.save(p, b)
        self.assertEqual(settings_mod.load(p)["levels"]["card_done"], "log")
        self.assertTrue(p.with_suffix(".yaml.bak").exists())

    def test_describe_is_plain_english(self):
        text = settings_mod.describe(s())
        self.assertIn("finished cards: put it in the morning brief", text)
        self.assertIn("Phone chat: not set", text)


if __name__ == "__main__":
    unittest.main()
