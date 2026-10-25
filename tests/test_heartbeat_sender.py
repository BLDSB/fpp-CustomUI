import json
import os
import subprocess
import unittest
from unittest import mock

os.environ.update(
    DATABASE_URL="sqlite://",
    SECRET_KEY="test-secret",
    ADMIN_PASSWORD_HASH="x",
    INTERNAL_TOKEN="tok-abc",
    FPP_BASE_URL="http://127.0.0.1:9/api",
    FPP_MEDIA_ROOT=os.path.join(os.path.dirname(__file__), "_no_media"),
)

from app import create_app  # noqa: E402
from app import heartbeat  # noqa: E402

STATUS = {"fppd": "running", "status_name": "playing", "version": "9.5.3",
          "current_playlist": {"playlist": "Evening Show"}}


def completed(stdout="", code=0):
    return subprocess.CompletedProcess([], code, stdout=stdout, stderr="")


class Sender(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = create_app()

    def collect(self, status=STATUS, temp=61.234, uptime=3600.5, disk=12.34,
                power=None, rev="abc1234"):
        power = {} if power is None else power
        with mock.patch.object(heartbeat, "_fetch_status", return_value=status), \
                mock.patch.object(heartbeat, "_cpu_temp", return_value=temp), \
                mock.patch.object(heartbeat, "_uptime", return_value=uptime), \
                mock.patch.object(heartbeat, "_disk_pct", return_value=disk), \
                mock.patch.object(heartbeat, "_power", return_value=power), \
                mock.patch.object(heartbeat, "_plugin_revision", return_value=rev):
            return heartbeat.collect(self.app)

    def test_report_matches_what_the_terms_describe(self):
        report = self.collect(power={"throttled": "0x0", "undervoltage": False,
                                     "undervoltage_ever": False})
        self.assertEqual(report, {
            "plugin": "abc1234", "fppd": "running", "state": "playing", "playlist": "Evening Show",
            "fpp": "9.5.3", "cpu_temp_c": 61.2, "uptime_s": 3600.5, "disk_pct": 12.3,
            "throttled": "0x0", "undervoltage": False, "undervoltage_ever": False})

    def test_report_carries_no_credentials_or_lighting_content(self):
        text = json.dumps(self.collect(status={**STATUS, "secret_pin": "PIN-9X7Z", "scenes": ["x"]}))
        for forbidden in ("PIN-9X7Z", "secret", "scenes", "password", "token"):
            self.assertNotIn(forbidden, text)

    def test_an_unreachable_player_is_reported_as_down(self):
        report = self.collect(status=None)
        self.assertEqual(report["fppd"], "down")
        self.assertNotIn("playlist", report)

    def test_missing_readings_are_left_out_not_faked(self):
        report = self.collect(temp=None, uptime=None, disk=None)
        for key in ("cpu_temp_c", "uptime_s", "disk_pct", "undervoltage"):
            self.assertNotIn(key, report)

    def test_power_flags_decode_vcgencmd(self):
        for raw, now, ever in (("throttled=0x0", False, False),
                               ("throttled=0x1", True, False),
                               ("throttled=0x50005", True, True),
                               ("throttled=0x50000", False, True),
                               ("throttled=0x80008", False, False)):   # the Backpack's value
            with mock.patch.object(heartbeat.subprocess, "run", return_value=completed(raw + "\n")):
                power = heartbeat._power()
            self.assertEqual((power["undervoltage"], power["undervoltage_ever"]), (now, ever), raw)

    def test_power_is_empty_where_vcgencmd_is_missing_or_odd(self):
        with mock.patch.object(heartbeat.subprocess, "run", side_effect=FileNotFoundError()):
            self.assertEqual(heartbeat._power(), {})
        with mock.patch.object(heartbeat.subprocess, "run", return_value=completed("nonsense")):
            self.assertEqual(heartbeat._power(), {})

    def test_send_posts_the_report_to_the_hub_over_the_tunnel(self):
        resp = mock.Mock()
        resp.raise_for_status.return_value = None
        with mock.patch.object(heartbeat, "collect", return_value={"fppd": "running"}), \
                mock.patch.object(heartbeat.requests, "post", return_value=resp) as post:
            self.assertTrue(heartbeat.send(self.app))
        url = post.call_args[0][0]
        self.assertEqual(url, "http://10.99.0.1:8081/api/heartbeat")
        self.assertEqual(json.loads(post.call_args[1]["data"]), {"fppd": "running"})
        self.assertLessEqual(post.call_args[1]["timeout"], 5)

    def _hub_url_with_state(self, state):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "state.json")
            if state is not None:
                with open(path, "w") as fh:
                    fh.write(state if isinstance(state, str) else json.dumps(state))
            with mock.patch.object(heartbeat, "STATE_FILE", path):
                return heartbeat.hub_url()

    def test_the_hub_address_comes_from_the_enrolment_record(self):
        self.assertEqual(self._hub_url_with_state({"hub_ip": "10.98.0.1", "heartbeat_port": 8091}),
                         "http://10.98.0.1:8091/api/heartbeat")

    def test_older_or_odd_records_fall_back_to_the_live_hub(self):
        live = "http://10.99.0.1:8081/api/heartbeat"
        for state in (None, "not json", {}, {"slug": "x"},
                      {"hub_ip": "192.168.1.1", "heartbeat_port": 8081},
                      {"hub_ip": "10.98.0.1", "heartbeat_port": 80},
                      {"hub_ip": "10.98.0.1", "heartbeat_port": "abc"}):
            self.assertEqual(self._hub_url_with_state(state), live, state)

    def test_a_failed_send_never_raises(self):
        with mock.patch.object(heartbeat, "collect", return_value={}), \
                mock.patch.object(heartbeat.requests, "post",
                                  side_effect=heartbeat.requests.ConnectionError("down")):
            self.assertFalse(heartbeat.send(self.app))
        bad = mock.Mock()
        bad.raise_for_status.side_effect = heartbeat.requests.HTTPError("403")
        with mock.patch.object(heartbeat, "collect", return_value={}), \
                mock.patch.object(heartbeat.requests, "post", return_value=bad):
            self.assertFalse(heartbeat.send(self.app))

    def test_nothing_is_sent_until_the_controller_is_enrolled(self):
        with mock.patch.object(heartbeat.os.path, "exists", return_value=False):
            self.assertFalse(heartbeat.enrolled())
        with mock.patch.object(heartbeat.os.path, "exists", return_value=True) as exists:
            self.assertTrue(heartbeat.enrolled())
        exists.assert_called_with("/var/lib/fpp-remote/state.json")

    def test_the_hub_address_is_the_tunnel_address_not_a_public_one(self):
        self.assertTrue(heartbeat.HUB_URL.startswith("http://10.99.0.1:"))


if __name__ == "__main__":
    unittest.main()
