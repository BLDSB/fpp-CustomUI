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
from app import remote_access  # noqa: E402
from app import ui_path as ui_path_mod  # noqa: E402
from app.security import _rate_hits  # noqa: E402

CODE = "Abcdefghij_klmnopqrstuvwxyz-0123456789"


def helper_result(stdout="", stderr="", code=0):
    return subprocess.CompletedProcess(["sudo"], code, stdout=stdout, stderr=stderr)


class RemoteCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = create_app()
        cls.app.config["TESTING"] = False

    def setUp(self):
        _rate_hits.clear()
        self.client = self.app.test_client()
        self.installed = mock.patch.object(remote_access, "available", return_value=True)
        self.installed.start()
        self.addCleanup(self.installed.stop)

    def login(self):
        with self.client.session_transaction() as s:
            s["logged_in"] = True


class Status(RemoteCase):
    def test_requires_login(self):
        self.assertIn(self.client.get("/api/remote/status").status_code, (302, 401))

    def test_hidden_when_the_helper_is_not_installed(self):
        self.login()
        with mock.patch.object(remote_access, "available", return_value=False):
            data = self.client.get("/api/remote/status").get_json()
        self.assertEqual(data, {"available": False})

    def test_reports_the_helpers_answer(self):
        self.login()
        out = json.dumps({"enrolled": True, "connected": True, "handshake_age": 12,
                          "slug": "forest", "address": "https://forest.example.test",
                          "ui_path": "/ui"})
        with mock.patch.object(remote_access.subprocess, "run", return_value=helper_result(out)):
            data = self.client.get("/api/remote/status").get_json()
        self.assertTrue(data["available"] and data["enrolled"] and data["connected"])
        self.assertEqual(data["address"], "https://forest.example.test")

    def test_a_broken_helper_gives_an_error_not_a_crash(self):
        self.login()
        with mock.patch.object(remote_access.subprocess, "run", return_value=helper_result("not json")):
            data = self.client.get("/api/remote/status").get_json()
        self.assertTrue(data["available"])
        self.assertIn("error", data)


class Enroll(RemoteCase):
    def post(self, code=CODE):
        return self.client.post("/api/remote/enroll", json={"code": code})

    def ok(self, ui_path="/ui", connected=True):
        return helper_result(json.dumps({"ok": True, "slug": "forest", "ui_path": ui_path,
                                         "address": "https://forest.example.test",
                                         "connected": connected}))

    def test_requires_login(self):
        with mock.patch.object(remote_access.subprocess, "run") as run:
            self.assertIn(self.post().status_code, (302, 401))
        run.assert_not_called()

    def test_only_the_validated_code_reaches_the_helper(self):
        self.login()
        with mock.patch.object(remote_access.subprocess, "run", return_value=self.ok()) as run, \
                mock.patch.object(ui_path_mod, "apply", return_value=None):
            r = self.post("  Abcdefghij_klmnopqrstuvwxyz-0123456789\n")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(run.call_args[0][0], ["sudo", "-n", remote_access.HELPER_BIN, "enroll", CODE])

    def test_the_whole_pasted_command_is_accepted_and_only_the_code_is_used(self):
        self.login()
        command = f"curl -fsSL https://portal.bldlightingcontrol.com/enroll.sh | sudo bash -s -- {CODE}"
        with mock.patch.object(remote_access.subprocess, "run", return_value=self.ok()) as run, \
                mock.patch.object(ui_path_mod, "apply", return_value=None):
            r = self.post(command)
        self.assertEqual(r.status_code, 200)
        self.assertEqual(run.call_args[0][0][-2:], ["enroll", CODE])

    def test_a_pasted_command_with_junk_for_a_code_is_still_refused(self):
        self.login()
        command = "curl -fsSL https://portal.bldlightingcontrol.com/enroll.sh | sudo bash -s -- ; id"
        with mock.patch.object(remote_access.subprocess, "run") as run:
            self.assertEqual(self.post(command).status_code, 400)
        run.assert_not_called()

    def test_bad_codes_never_run_the_helper(self):
        self.login()
        bad = ["", "short", "a" * 19, "a" * 65, CODE + ";id", CODE + "$(id)", "../" + CODE,
               CODE + " extra words", None, 12345]
        with mock.patch.object(remote_access.subprocess, "run") as run:
            for code in bad:
                _rate_hits.clear()                       # the limiter has its own test below
                self.assertEqual(self.post(code).status_code, 400, repr(code))
        run.assert_not_called()

    def test_moves_to_the_portals_path_when_it_differs(self):
        self.login()
        with mock.patch.object(remote_access.subprocess, "run", return_value=self.ok("/ui")), \
                mock.patch.object(ui_path_mod, "current_path", return_value="CustomUI"), \
                mock.patch.object(ui_path_mod, "apply", return_value=None) as apply:
            data = self.post().get_json()
        apply.assert_called_once_with("ui")
        self.assertEqual(data["url"], "/ui/")
        self.assertTrue(data["ok"] and data["connected"])

    def test_leaves_the_path_alone_when_it_already_matches(self):
        self.login()
        with mock.patch.object(remote_access.subprocess, "run", return_value=self.ok("/Inova")), \
                mock.patch.object(ui_path_mod, "current_path", return_value="Inova"), \
                mock.patch.object(ui_path_mod, "apply") as apply:
            data = self.post().get_json()
        apply.assert_not_called()
        self.assertNotIn("url", data)

    def test_a_path_that_cannot_be_applied_is_a_warning_not_a_failure(self):
        self.login()
        with mock.patch.object(remote_access.subprocess, "run", return_value=self.ok("/media")), \
                mock.patch.object(ui_path_mod, "current_path", return_value="CustomUI"):
            r = self.post()
        data = r.get_json()
        self.assertEqual(r.status_code, 200)             # the tunnel is up
        self.assertIn("reserved", data["warning"])
        self.assertNotIn("url", data)

    def test_helper_failures_are_shown_to_the_user(self):
        self.login()
        failed = helper_result(stderr="noise\nThe portal did not accept that code.", code=1)
        with mock.patch.object(remote_access.subprocess, "run", return_value=failed):
            r = self.post()
        self.assertEqual(r.status_code, 400)
        self.assertEqual(r.get_json()["error"], "The portal did not accept that code.")

    def test_timeout_is_reported(self):
        self.login()
        with mock.patch.object(remote_access.subprocess, "run",
                               side_effect=subprocess.TimeoutExpired("sudo", 150)):
            r = self.post()
        self.assertEqual(r.status_code, 400)
        self.assertIn("Timed out", r.get_json()["error"])

    def test_missing_helper_has_a_clear_message(self):
        self.login()
        with mock.patch.object(remote_access, "available", return_value=False):
            r = self.post()
        self.assertEqual(r.status_code, 400)
        self.assertIn("not installed", r.get_json()["error"])

    def test_guessing_codes_is_rate_limited(self):
        self.login()
        with mock.patch.object(remote_access.subprocess, "run",
                               return_value=helper_result(stderr="no", code=1)):
            statuses = [self.post().status_code for _ in range(7)]
        self.assertEqual(statuses[:5], [400] * 5)
        self.assertEqual(statuses[5:], [429, 429])

    def test_cross_site_requests_are_blocked(self):
        self.login()
        with mock.patch.object(remote_access.subprocess, "run") as run:
            r = self.client.post("/api/remote/enroll", json={"code": CODE},
                                 headers={"Origin": "http://evil.example"})
        self.assertEqual(r.status_code, 403)
        run.assert_not_called()


class Disconnect(RemoteCase):
    def test_requires_login(self):
        self.assertIn(self.client.post("/api/remote/disconnect").status_code, (302, 401))

    def test_disconnects_through_the_helper(self):
        self.login()
        with mock.patch.object(remote_access.subprocess, "run",
                               return_value=helper_result('{"ok": true}')) as run:
            r = self.client.post("/api/remote/disconnect")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(run.call_args[0][0], ["sudo", "-n", remote_access.HELPER_BIN, "disconnect"])

    def test_failures_are_reported(self):
        self.login()
        with mock.patch.object(remote_access.subprocess, "run",
                               return_value=helper_result(stderr="boom", code=1)):
            r = self.client.post("/api/remote/disconnect")
        self.assertEqual(r.status_code, 400)


class SettingsPage(RemoteCase):
    def test_the_card_and_script_are_on_the_settings_page(self):
        self.login()
        html = self.client.get("/settings").get_data(as_text=True)
        self.assertIn('id="remote-card"', html)
        self.assertIn("/api/remote/enroll", html)
        self.assertIn("/api/remote/status", html)


if __name__ == "__main__":
    unittest.main()
