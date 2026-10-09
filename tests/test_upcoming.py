import time
import unittest
from datetime import datetime, timedelta

from app import upcoming as up
from app.alert_monitor import _recipients


def ts(days, hour=19, base=None):
    base = base or datetime(2026, 11, 20, 9, 30)
    return (base.replace(hour=hour, minute=0) + timedelta(days=days)).timestamp()


NOW = datetime(2026, 11, 20, 9, 30).timestamp()


def start(name, days, hour=19):
    t = ts(days, hour)
    return {"name": name, "start": t, "end": t + 3 * 3600}


class Due(unittest.TestCase):
    def due(self, starts, active=None, notified=None, lead=1, gap=14):
        return up.due_reminders(starts, NOW, lead_days=lead, gap_days=gap,
                                last_active=active or {}, notified=notified or {})

    def test_remind_after_gap(self):
        d = self.due([start("Xmas", 1)], active={"Xmas": "2026-10-01"})
        self.assertEqual([x["name"] for x in d], ["Xmas"])
        self.assertEqual(d[0]["kind"], "new")

    def test_never_seen_counts_as_gap(self):
        self.assertEqual(len(self.due([start("Xmas", 1)])), 1)

    def test_nightly_show_not_reminded(self):
        self.assertEqual(self.due([start("Nightly", 1)], active={"Nightly": "2026-11-19"}), [])

    def test_not_yet_within_lead_time(self):
        self.assertEqual(self.due([start("Xmas", 5)]), [])

    def test_already_notified_same_day(self):
        n = {"Xmas": {"date": "2026-11-21", "sent": "2026-11-20"}}
        self.assertEqual(self.due([start("Xmas", 1)], notified=n), [])

    def test_date_moved_resends_as_changed(self):
        n = {"Xmas": {"date": "2026-11-21", "sent": "2026-11-20"}}
        d = self.due([start("Xmas", 2)], notified=n, lead=2)
        self.assertEqual(d[0]["kind"], "changed")
        self.assertEqual(d[0]["previous"], "2026-11-21")

    def test_old_record_is_a_fresh_run(self):
        n = {"Xmas": {"date": "2026-01-02", "sent": "2026-01-01"}}
        self.assertEqual(len(self.due([start("Xmas", 1)], notified=n, active={"Xmas": "2026-01-10"})), 1)

    def test_two_shows_same_day(self):
        d = self.due([start("A", 1), start("B", 1, hour=20)])
        self.assertEqual({x["name"] for x in d}, {"A", "B"})

    def test_only_first_start_considered(self):
        d = self.due([start("Xmas", 1), start("Xmas", 2), start("Xmas", 3)])
        self.assertEqual(len(d), 1)

    def test_mark_notified_prevents_repeat(self):
        s = [start("Xmas", 1)]
        d = self.due(s)
        n = up.mark_notified({}, d, NOW)
        self.assertEqual(self.due(s, notified=n), [])


class Baseline(unittest.TestCase):
    def test_in_progress_shows_are_not_announced(self):
        s = [start("Nightly", 1), start("Later", 10)]
        active, notified = up.baseline(s, NOW, 1)
        self.assertIn("Nightly", notified)
        self.assertNotIn("Later", notified)
        d = up.due_reminders(s, NOW, lead_days=1, gap_days=14, last_active=active, notified=notified)
        self.assertEqual(d, [])


class Starts(unittest.TestCase):
    def test_filters_commands_and_disabled(self):
        sched = {
            "entries": [{"id": 1, "enabled": 1}, {"id": 2, "enabled": 0}],
            "items": [
                {"id": 1, "command": "Start Playlist", "args": ["A"], "startTime": 100, "endTime": 200, "priority": 0},
                {"id": 2, "command": "Start Playlist", "args": ["B"], "startTime": 110, "endTime": 200, "priority": 0},
                {"id": 1, "command": "Stop Now", "args": [], "startTime": 150},
            ],
        }
        self.assertEqual([s["name"] for s in up.playlist_starts(sched)], ["A"])

    def test_shadowed_start_dropped(self):
        sched = {"entries": [], "items": [
            {"command": "Start Playlist", "args": ["A"], "startTime": 100, "endTime": 500, "priority": 0},
            {"command": "Start Playlist", "args": ["B"], "startTime": 200, "endTime": 300, "priority": 1},
        ]}
        self.assertEqual([s["name"] for s in up.playlist_starts(sched)], ["A"])


class Preflight(unittest.TestCase):
    def test_flags_empty_and_missing_files(self):
        pl = {"mainPlaylist": [
            {"type": "both", "sequenceName": "a.fseq", "mediaName": "a.mp3"},
        ]}
        self.assertEqual(up.preflight("P", lambda n: pl, {"a"}, {"a.mp3"}), [])
        out = up.preflight("P", lambda n: pl, set(), set())
        self.assertEqual(len(out), 2)
        self.assertTrue(up.preflight("P", lambda n: {"mainPlaylist": []}, set(), set()))
        self.assertTrue(up.preflight("P", lambda n: None, set(), set()))

    def test_unknown_listing_skips_check(self):
        pl = {"mainPlaylist": [{"type": "both", "sequenceName": "a.fseq", "mediaName": "a.mp3"}]}
        self.assertEqual(up.preflight("P", lambda n: pl, None, None), [])


class Email(unittest.TestCase):
    def test_single_and_flagged_subject(self):
        d = [{**start("Xmas", 1), "kind": "new", "previous": None}]
        subj, body = up.build_reminder(d, {"Xmas": ["Sequence file missing: x"]}, {}, NOW)
        self.assertIn("tomorrow", subj)
        self.assertIn("needs attention", subj)
        self.assertIn("Sequence file missing", body)


class Agenda(unittest.TestCase):
    def test_lists_window_grouped_by_day(self):
        starts = [start("A", 1), start("B", 1, hour=21), start("C", 10), start("Far", 40)]
        lines = up.build_agenda(starts, NOW, 28)
        text = "\n".join(lines)
        self.assertIn("A", text)
        self.assertIn("B", text)
        self.assertIn("C", text)
        self.assertNotIn("Far", text)
        self.assertEqual(lines.count(""), 1)  # two date groups

    def test_included_in_email(self):
        d = [{**start("A", 1), "kind": "new", "previous": None}]
        _, body = up.build_reminder(d, {}, {}, NOW, agenda=up.build_agenda([start("A", 1)], NOW), agenda_days=28)
        self.assertIn("Everything scheduled in the next 28 days", body)
        _, body = up.build_reminder(d, {}, {}, NOW, agenda=[], agenda_days=28)
        self.assertIn("Nothing scheduled.", body)


class Recipients(unittest.TestCase):
    S = {"alert_email_to": "a@x.com", "alert_email_to_2": "b@x.com", "alert_email_to_3": "c@x.com"}

    def test_default_all_on(self):
        self.assertEqual(_recipients(self.S, "upcoming"), ["a@x.com", "b@x.com", "c@x.com"])

    def test_per_kind_toggle(self):
        s = {**self.S, "alert_to2_upcoming": "0", "alert_to3_missed": "0"}
        self.assertEqual(_recipients(s, "upcoming"), ["a@x.com", "c@x.com"])
        self.assertEqual(_recipients(s, "missed"), ["a@x.com", "b@x.com"])
        self.assertEqual(len(_recipients(s)), 3)

    def test_duplicate_address_on_if_any_slot_on(self):
        s = {"alert_email_to": "a@x.com", "alert_email_to_2": "a@x.com", "alert_to1_upcoming": "0"}
        self.assertEqual(_recipients(s, "upcoming"), ["a@x.com"])


if __name__ == "__main__":
    unittest.main()
