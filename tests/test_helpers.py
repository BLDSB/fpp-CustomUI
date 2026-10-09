"""Unit tests for the pure business-logic helpers (no network, no database)."""
import unittest
from datetime import date
from types import SimpleNamespace

import bcrypt

from app import alert_monitor as am
from app import fpp_playlist as fp
from app import overlay_layout as ol
from app import ui_path
from app.models import MAX_ZONES, OVERLAY_MODELS, is_managed_overlay
from app.routes import auth
from app.routes import scheduler as sch


# ── FPP playlist builders ────────────────────────────────────────────────────
# The playlist shape was found by trial on hardware (see app/fpp_playlist.py);
# these tests pin the load-bearing parts so an edit cannot silently reshape it.

class PlaylistShape(unittest.TestCase):
    def test_url_command_goes_in_main_playlist_and_lead_in_is_empty(self):
        pl = fp.build_playlist_def("Scene - X", [fp.url_cmd("http://x")], "d")
        self.assertEqual(pl["leadIn"], [])
        self.assertEqual(pl["mainPlaylist"][0]["command"], "URL")
        self.assertEqual(pl["version"], 4)

    def test_lead_out_is_pause_then_stop_effects(self):
        out = fp.build_playlist_def("p", [], "d")["leadOut"]
        self.assertEqual(out[0]["type"], "pause")
        self.assertEqual(out[0]["duration"], 3)
        self.assertEqual(out[1]["command"], "Overlay Model Effect")
        self.assertEqual(out[1]["args"], ["--All Models--", "Enabled", "Stop Effects"])

    def test_repeat_flag(self):
        self.assertEqual(fp.build_playlist_def("p", [], "d", repeat=True)["repeat"], 1)
        self.assertEqual(fp.build_playlist_def("p", [], "d", repeat=False)["repeat"], 0)

    def test_sequence_entry_adds_extension_and_only_sets_duration_when_given(self):
        self.assertEqual(fp.sequence_entry("Song")["sequenceName"], "Song.fseq")
        self.assertEqual(fp.sequence_entry("Song.fseq")["sequenceName"], "Song.fseq")
        self.assertNotIn("duration", fp.sequence_entry("Song"))
        self.assertEqual(fp.sequence_entry("Song", 90)["duration"], 90)

    def test_pause_item(self):
        self.assertEqual(fp.pause_item(7)["duration"], 7)


# ── Overlay layout geometry ──────────────────────────────────────────────────

MAP = """\
# Model: 'Tree', 4 nodes
0,0,0,0,3,RGB,2
10,0,0,3,3,RGB,2
0,10,0,6,3,RGB,2
10,10,0,9,3,RGB,2
# Model: 'Star', 1 nodes
50,50,0,12,3,RGB,2
"""


class DisplayMap(unittest.TestCase):
    def test_parse_models_and_nodes(self):
        models = ol.parse_display_map(MAP)
        self.assertEqual([m["name"] for m in models], ["Tree", "Star"])
        self.assertEqual(len(models[0]["nodes"]), 4)
        self.assertEqual(models[0]["channels_per_node"], 3)

    def test_garbage_lines_are_skipped_not_fatal(self):
        text = "1651,1343\nnot,a,node\n# Model: 'A', 1 nodes\n0,0,0,0,3,RGB,2\n0,0\n"
        models = ol.parse_display_map(text)
        self.assertEqual([m["name"] for m in models], ["A"])

    def test_model_with_no_nodes_is_dropped(self):
        self.assertEqual(ol.parse_display_map("# Model: 'Empty', 0 nodes\n"), [])

    def test_derive_grid_is_flipped_so_row_zero_is_the_top(self):
        grid = ol.derive_grid(ol.parse_display_map(MAP)[0])
        self.assertEqual((grid["width"], grid["height"]), (2, 2))
        self.assertEqual(grid["start_channel"], 1)          # FPP is 1-based
        self.assertEqual(grid["channels_per_node"], 3)
        self.assertEqual(grid["placed"], 4)
        # y runs bottom-up in the map: the nodes at y=10 (channels 6, 9) are the top row.
        self.assertEqual(grid["data"], "3,4;1,2")

    def test_validate_grid_accepts_good_and_rejects_bad(self):
        grid = ol.derive_grid(ol.parse_display_map(MAP)[0])
        self.assertIsNone(ol.validate_grid(grid))
        self.assertIn("outside", ol.validate_grid(dict(grid, data="99,1;1,1")))
        self.assertIn("no cells", ol.validate_grid(dict(grid, width=0)))
        self.assertIn("limit", ol.validate_grid(dict(grid, width=10 ** 6, height=10 ** 6)))

    def test_xlights_custom_model(self):
        grid = ol.parse_xlights_custom("1,2;3,", 100, 3)
        self.assertEqual(grid["start_channel"], 100)
        self.assertEqual(grid["data"], "1,2;3,")
        self.assertEqual(grid["channel_count"], 9)          # 3 nodes x 3 channels

    def test_xlights_custom_rejects_junk(self):
        with self.assertRaises(ValueError):
            ol.parse_xlights_custom("1,x", 1)
        with self.assertRaises(ValueError):
            ol.parse_xlights_custom("", 1)

    def test_to_fpp_model_and_mask(self):
        grid = ol.parse_xlights_custom("1,;,2", 1, 3)
        model = ol.to_fpp_model("Zone 1", grid)
        self.assertEqual(model["Name"], "Zone 1")
        self.assertEqual(model["Orientation"], "custom")
        self.assertEqual(model["StartChannel"], 1)
        self.assertEqual(ol.grid_mask(grid["data"]), ["10", "01"])


class ZoneGrouping(unittest.TestCase):
    def test_few_models_keep_their_own_zone(self):
        labels, group_of = ol.suggest_groups(["A", "B"], 15)
        self.assertEqual(labels, ["A", "B"])
        self.assertEqual(group_of, [0, 1])

    def test_numbered_models_group_by_prefix(self):
        names = [f"Inside {i}" for i in range(1, 17)] + [f"Outside {i}" for i in range(1, 17)]
        labels, group_of = ol.suggest_groups(names, 15)
        self.assertEqual(labels, ["Inside", "Outside"])
        self.assertEqual(set(group_of[:16]), {0})
        self.assertEqual(set(group_of[16:]), {1})

    def test_too_many_prefixes_fall_back_to_even_chunks(self):
        names = [f"Unique{chr(65 + i)}{chr(97 + i)}" for i in range(20)]
        labels, group_of = ol.suggest_groups(names, 5)
        self.assertEqual(len(labels), 5)
        self.assertEqual(len(group_of), 20)
        self.assertLessEqual(max(group_of), 4)


# ── Models ───────────────────────────────────────────────────────────────────

class OverlayNames(unittest.TestCase):
    def test_managed_overlay_names(self):
        self.assertTrue(is_managed_overlay("All"))
        self.assertTrue(is_managed_overlay("Zone 3"))
        self.assertTrue(is_managed_overlay(f"Zone {MAX_ZONES}.2"))
        self.assertFalse(is_managed_overlay(f"Zone {MAX_ZONES + 1}"))
        self.assertFalse(is_managed_overlay("Roofline"))
        self.assertFalse(is_managed_overlay(None))

    def test_overlay_model_set_covers_all_and_every_zone(self):
        self.assertIn("All", OVERLAY_MODELS)
        self.assertIn("Zone 1", OVERLAY_MODELS)
        self.assertIn(f"Zone {MAX_ZONES}", OVERLAY_MODELS)


# ── Scheduling ───────────────────────────────────────────────────────────────

class ScheduleDates(unittest.TestCase):
    def holiday(self, sm, sd, em, ed):
        return SimpleNamespace(start_month=sm, start_day=sd, end_month=em, end_day=ed)

    def test_parse_date(self):
        self.assertEqual(sch._parse_date("2026-12-25"), "2026-12-25")
        self.assertEqual(sch._parse_date(""), "")
        self.assertEqual(sch._parse_date(None), "")
        self.assertIsNone(sch._parse_date("12/25/2026"))
        self.assertIsNone(sch._parse_date("2026-13-01"))

    def test_holiday_this_year_when_not_yet_over(self):
        xmas = self.holiday(12, 1, 12, 26)
        self.assertEqual(sch.resolve_holiday_dates(xmas, today=date(2026, 10, 9)),
                         ("2026-12-01", "2026-12-26"))

    def test_finished_holiday_rolls_to_next_year(self):
        xmas = self.holiday(12, 1, 12, 26)
        self.assertEqual(sch.resolve_holiday_dates(xmas, today=date(2026, 12, 27)),
                         ("2027-12-01", "2027-12-26"))

    def test_range_spanning_new_year(self):
        winter = self.holiday(12, 20, 1, 5)
        self.assertEqual(sch.resolve_holiday_dates(winter, today=date(2027, 1, 2)),
                         ("2026-12-20", "2027-01-05"))
        self.assertEqual(sch.resolve_holiday_dates(winter, today=date(2026, 6, 1)),
                         ("2026-12-20", "2027-01-05"))

    def test_feb_29_is_clamped_in_a_common_year(self):
        leap = self.holiday(2, 29, 3, 1)
        self.assertEqual(sch.resolve_holiday_dates(leap, today=date(2027, 1, 1))[0], "2027-02-28")


class ScheduleValidation(unittest.TestCase):
    GOOD = {"playlist": "Show", "day": 7, "startTime": "18:00:00", "endTime": "22:00:00",
            "startDate": "2026-11-01", "endDate": "2026-12-31"}

    def test_valid_entry_gets_defaults(self):
        entry, err = sch._validate(dict(self.GOOD))
        self.assertIsNone(err)
        self.assertEqual(entry["enabled"], 1)
        self.assertEqual(entry["repeat"], 0)
        self.assertEqual(entry["stopType"], 0)
        self.assertEqual(entry["startTimeOffset"], 0)

    def test_solar_labels_and_offsets(self):
        entry, err = sch._validate(dict(self.GOOD, startTime="Dusk", endTime="SunRise", startTimeOffset=-30))
        self.assertIsNone(err)
        self.assertEqual(entry["startTimeOffset"], -30)

    def test_each_field_is_rejected_with_a_message(self):
        bad = [
            ({"playlist": "", "command": ""}, "playlist or command"),
            ({"day": 99}, "day"),
            ({"startTime": "25:99"}, "startTime"),
            ({"endTime": "soon"}, "endTime"),
            ({"startTimeOffset": 2000}, "startTimeOffset"),
            ({"repeat": 5}, "repeat"),
            ({"enabled": 9}, "enabled"),
            ({"stopType": 7}, "stopType"),
            ({"startDate": "garbage"}, "startDate"),
            ({"startDate": "2027-01-01", "endDate": "2026-01-01"}, "after"),
        ]
        for patch, needle in bad:
            entry, err = sch._validate(dict(self.GOOD, **patch))
            self.assertIsNone(entry, patch)
            self.assertIn(needle, err, patch)

    def test_command_entry_keeps_args_only_as_a_list(self):
        entry, _ = sch._validate(dict(self.GOOD, playlist="", command="Run", args=["a"]))
        self.assertEqual(entry["args"], ["a"])
        entry, _ = sch._validate(dict(self.GOOD, playlist="", command="Run", args="oops"))
        self.assertEqual(entry["args"], [])


# ── Alert monitor helpers ────────────────────────────────────────────────────

class MonitorHelpers(unittest.TestCase):
    def test_to_int(self):
        self.assertEqual(am._to_int("5", 1), 5)
        self.assertEqual(am._to_int(" 7 ", 1), 7)
        self.assertEqual(am._to_int("junk", 3), 3)
        self.assertEqual(am._to_int(None, 3), 3)
        self.assertEqual(am._to_int("0", 3, lo=1), 3)
        self.assertEqual(am._to_int("2000", 3, hi=1440), 3)

    def test_recipients_split_dedupe_and_respect_opt_out(self):
        settings = {
            "alert_email_to": "a@x.com; b@x.com",
            "alert_email_to_2": "A@x.com",
            "alert_email_to_3": "c@x.com",
            "alert_to3_missed": "0",
        }
        self.assertEqual(am._recipients(settings), ["a@x.com", "b@x.com", "c@x.com"])
        self.assertEqual(am._recipients(settings, "missed"), ["a@x.com", "b@x.com"])
        self.assertEqual(am._recipients({}), [])

    def test_next_start(self):
        status = {"scheduler": {"nextPlaylist": {"playlistName": " Show ", "scheduledStartTime": "1800000000"}}}
        self.assertEqual(am._next_start(status), ("Show", 1800000000))
        self.assertIsNone(am._next_start({}))
        self.assertIsNone(am._next_start({"scheduler": {"nextPlaylist": {"playlistName": "X", "scheduledStartTime": 0}}}))


class EntryRunsToday(unittest.TestCase):
    FRIDAY = date(2026, 10, 9)

    def test_enum_everyday_and_weekday_rules(self):
        base = {"enabled": 1, "startDate": "2026-01-01", "endDate": "2026-12-31"}
        self.assertTrue(am._entry_runs_today(dict(base, day=7), self.FRIDAY))      # everyday
        self.assertTrue(am._entry_runs_today(dict(base, day=8), self.FRIDAY))      # Mon-Fri
        self.assertFalse(am._entry_runs_today(dict(base, day=9), self.FRIDAY))     # Sat/Sun
        self.assertTrue(am._entry_runs_today(dict(base, day=5), self.FRIDAY))      # Friday only
        self.assertFalse(am._entry_runs_today(dict(base, day=1), self.FRIDAY))     # Monday only

    def test_disabled_and_out_of_range_dates_never_run(self):
        self.assertFalse(am._entry_runs_today({"enabled": 0, "day": 7}, self.FRIDAY))
        self.assertFalse(am._entry_runs_today(
            {"enabled": 1, "day": 7, "startDate": "2027-01-01", "endDate": "2027-02-01"}, self.FRIDAY))

    def test_odd_even_days_alternate(self):
        entry = {"enabled": 1, "startDate": "2026-01-01", "endDate": "2026-12-31"}
        odd = am._entry_runs_today(dict(entry, day=14), self.FRIDAY)
        even = am._entry_runs_today(dict(entry, day=15), self.FRIDAY)
        self.assertNotEqual(odd, even)

    def test_garbage_day_is_off_rather_than_a_crash(self):
        self.assertFalse(am._entry_runs_today({"enabled": 1, "day": "x"}, self.FRIDAY))


# ── Install path ─────────────────────────────────────────────────────────────

class UiPath(unittest.TestCase):
    def test_validate(self):
        self.assertIsNone(ui_path.validate("cityname"))
        self.assertIsNone(ui_path.validate("Show_2-b"))
        for bad in ("", "has space", "a/b", "../x", "x" * 33, "api", "FPP", "plugins"):
            self.assertIsNotNone(ui_path.validate(bad), bad)


# ── PIN checking and login throttling ────────────────────────────────────────

class PinChecks(unittest.TestCase):
    def test_check_pin_matches_only_the_right_pin(self):
        stored = bcrypt.hashpw(b"1234", bcrypt.gensalt(4)).decode()
        self.assertTrue(auth._check_pin("1234", stored))
        self.assertFalse(auth._check_pin("1235", stored))
        self.assertFalse(auth._check_pin("1234", ""))

    def test_corrupt_hash_is_a_non_match_not_a_crash(self):
        from app import create_app
        app = create_app()
        with app.app_context():
            self.assertFalse(auth._check_pin("1234", "not-a-bcrypt-hash"))

    def test_lockout_starts_after_free_attempts_and_clears(self):
        ip = "203.0.113.9"
        auth._clear_login_failures(ip)
        for _ in range(auth._FREE_ATTEMPTS):
            auth._record_login_failure(ip)
        self.assertEqual(auth._throttle_wait(ip), 0)
        auth._record_login_failure(ip)
        self.assertGreater(auth._throttle_wait(ip), 0)
        auth._clear_login_failures(ip)
        self.assertEqual(auth._throttle_wait(ip), 0)


# ── Small shared helpers ─────────────────────────────────────────────────────

class SharedHelpers(unittest.TestCase):
    def test_fpp_urls_and_colors(self):
        from app import create_app
        from app.fpp_api import fpp_url, hex_to_rgb, playlist_url
        app = create_app()
        with app.app_context():
            base = app.config["FPP_BASE_URL"]
            self.assertEqual(fpp_url("/fppd/status"), f"{base}/fppd/status")
            self.assertEqual(playlist_url("Scene - A/B?"), f"{base}/playlist/Scene%20-%20A%2FB%3F")
        self.assertEqual(hex_to_rgb("#ff8800"), (255, 136, 0))
        self.assertEqual(hex_to_rgb("00ff00"), (0, 255, 0))

    def test_error_text_never_leaks_the_address(self):
        import requests
        from app import create_app
        from app.fpp_api import fpp_error_text
        app = create_app()
        with app.app_context():
            text = fpp_error_text(requests.ConnectionError("HTTPConnectionPool(host='10.1.2.3')"))
            self.assertNotIn("10.1.2.3", text)
            self.assertIn("reach", text)
            self.assertIn("too long", fpp_error_text(requests.Timeout("x")))

    def test_validation_helpers(self):
        from flask import Flask
        from app.validation import json_object, page_args, str_field
        app = Flask(__name__)
        with app.test_request_context("/x?limit=5&offset=2", json=[1, 2]):
            self.assertEqual(json_object(), {})
            self.assertEqual(page_args(), (5, 2))
        with app.test_request_context("/x", json={"a": 1}):
            self.assertEqual(json_object(), {"a": 1})
        with app.test_request_context("/x?limit=zzz&offset=-3"):
            self.assertEqual(page_args(default_limit=50), (50, 0))
        self.assertEqual(str_field({"n": "  hi  "}, "n"), "hi")
        self.assertEqual(str_field({"n": 5}, "n"), "")
        self.assertEqual(str_field({"n": "abcdef"}, "n", 3), "abc")


if __name__ == "__main__":
    unittest.main()
