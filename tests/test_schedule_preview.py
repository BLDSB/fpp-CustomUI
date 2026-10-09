"""Schedule Preview: which entries actually play, and which get cut short.

Priority is an entry's position in the schedule (0 wins). fppd lists every
start; the preview works out the interruptions.
"""
import unittest
from datetime import datetime

from app.routes.scheduler import preview_events


def at(day, hour, minute=0):
    return int(datetime(2026, 10, day, hour, minute).timestamp())


def start(entry_id, name, priority, begin, end):
    return {"id": entry_id, "command": "Start Playlist", "args": [name, "true", "false"],
            "priority": priority, "startTime": begin, "endTime": end}


def rows(items):
    return {(e["name"], e["start"]): e for e in preview_events(items, {}) if e["kind"] == "playlist"}


class CutShort(unittest.TestCase):
    def test_higher_priority_starting_mid_run_cuts_the_earlier_one(self):
        """The reported schedule: scene 7-10 PM (priority 2), playlist 9:10-11:11 PM (priority 1)."""
        items = [start(2, "Scene - All of the colors", 2, at(9, 19), at(9, 22)),
                 start(1, "Playlist 1", 1, at(9, 21, 10), at(9, 23, 11))]
        r = rows(items)
        scene = r[("Scene - All of the colors", "7:00 PM")]
        playlist = r[("Playlist 1", "9:10 PM")]
        self.assertEqual(scene["cutAt"], "9:10 PM")
        self.assertEqual(scene["cutBy"], "Playlist 1")
        self.assertEqual(scene["cutDate"], "2026-10-09")
        self.assertEqual(scene["end"], "10:00 PM")             # still the scheduled end, for "was until"
        self.assertFalse(scene["skipped"])
        self.assertNotIn("cutAt", playlist)
        self.assertFalse(playlist["skipped"])

    def test_lower_priority_starting_mid_run_is_skipped_and_names_what_blocks_it(self):
        items = [start(1, "Scene A", 1, at(9, 19), at(9, 22)),
                 start(2, "Playlist B", 2, at(9, 21, 10), at(9, 23, 11))]
        r = rows(items)
        self.assertTrue(r[("Playlist B", "9:10 PM")]["skipped"])
        self.assertEqual(r[("Playlist B", "9:10 PM")]["skippedBy"], "Scene A")
        self.assertNotIn("cutAt", r[("Scene A", "7:00 PM")])

    def test_equal_priority_does_not_interrupt(self):
        items = [start(1, "First", 1, at(9, 19), at(9, 22)),
                 start(1, "Second", 1, at(9, 20), at(9, 23))]
        r = rows(items)
        self.assertTrue(r[("Second", "8:00 PM")]["skipped"])
        self.assertNotIn("cutAt", r[("First", "7:00 PM")])

    def test_back_to_back_entries_do_not_interfere(self):
        items = [start(2, "Early", 2, at(9, 18), at(9, 20)),
                 start(1, "Late", 1, at(9, 20), at(9, 22))]       # starts exactly when Early ends
        r = rows(items)
        self.assertNotIn("cutAt", r[("Early", "6:00 PM")])
        self.assertFalse(r[("Late", "8:00 PM")]["skipped"])

    def test_cut_on_the_next_day_reports_that_date(self):
        items = [start(2, "Overnight", 2, at(9, 23), at(10, 3)),
                 start(1, "Priority", 1, at(10, 0, 30), at(10, 1))]
        night = rows(items)[("Overnight", "11:00 PM")]
        self.assertEqual(night["cutAt"], "12:30 AM")
        self.assertEqual(night["cutDate"], "2026-10-10")
        self.assertEqual(night["date"], "2026-10-09")

    def test_a_chain_of_takeovers_cuts_each_in_turn(self):
        items = [start(3, "Low", 3, at(9, 18), at(9, 23)),
                 start(2, "Mid", 2, at(9, 19), at(9, 22)),
                 start(1, "High", 1, at(9, 20), at(9, 21))]
        r = rows(items)
        self.assertEqual(r[("Low", "6:00 PM")]["cutAt"], "7:00 PM")
        self.assertEqual(r[("Low", "6:00 PM")]["cutBy"], "Mid")
        self.assertEqual(r[("Mid", "7:00 PM")]["cutAt"], "8:00 PM")
        self.assertEqual(r[("Mid", "7:00 PM")]["cutBy"], "High")
        self.assertNotIn("cutAt", r[("High", "8:00 PM")])

    def test_a_skipped_entry_never_cuts_anything(self):
        items = [start(1, "Boss", 1, at(9, 19), at(9, 23)),
                 start(2, "Meek", 2, at(9, 20), at(9, 22))]
        self.assertNotIn("cutAt", rows(items)[("Boss", "7:00 PM")])

    def test_commands_are_passed_through_and_never_cut_anything(self):
        items = [start(1, "Show", 1, at(9, 19), at(9, 22)),
                 {"id": 5, "command": "Volume Set", "args": ["70"], "startTime": at(9, 20)}]
        events = preview_events(items, {})
        self.assertEqual(events[1]["kind"], "command")
        self.assertNotIn("cutAt", events[0])

    def test_empty_schedule(self):
        self.assertEqual(preview_events([], {}), [])


if __name__ == "__main__":
    unittest.main()
