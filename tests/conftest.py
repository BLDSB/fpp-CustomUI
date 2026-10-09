"""Shared test setup.

Environment first: ``app.config`` calls ``load_dotenv()`` at import time, and a
developer's real ``.env`` (live PIN hashes, the Pi's address) must never leak
into a test run. dotenv does not override variables that are already set, so
pinning every key here is what isolates the suite.

``FakeFPP`` stands in for the player's REST API so the journey tests can
exercise real routes end to end without a controller.
"""
import os
import re
from urllib.parse import unquote
from unittest import mock

import pytest

os.environ.update(
    DATABASE_URL="sqlite://",
    SECRET_KEY="test-secret",
    ADMIN_PASSWORD_HASH="x",
    MASTER_PIN_HASH="",
    INTERNAL_TOKEN="tok-abc",
    UI_PATH="CustomUI",
    FPP_BASE_URL="http://127.0.0.1:9/api",
    FPP_MEDIA_ROOT=os.path.join(os.path.dirname(__file__), "_no_media"),
)


class _Response:
    def __init__(self, body=None, status=200):
        self._body = {} if body is None else body
        self.status_code = status
        self.ok = status < 400
        self.text = str(self._body)

    def json(self):
        return self._body

    def raise_for_status(self):
        if not self.ok:
            import requests
            raise requests.HTTPError(f"HTTP {self.status_code}", response=self)


class FakeFPP:
    """In-memory FPP: records every call, keeps a schedule and a playlist store."""

    def __init__(self):
        self.calls = []            # (method, path, json)
        self.playlists = {}        # name -> definition
        self.schedule = []
        self.overlay_state = {}    # model -> {"State": n, "RGB": [...]}
        self.status = {"status_name": "idle", "current_playlist": {"playlist": ""}}
        self.fppd_schedule = {"enabled": 1, "entries": [], "items": [], "scheduleDistance": 28}
        self.models = ["All"] + ["Zone %d" % i for i in range(1, 16)]
        # Real-controller quirk (see app/fpp_api.py): the first switch-on after a
        # "Stop Effects" command is acknowledged with 200 but ignored.
        self.ignore_first_on_after_stop = False
        self.never_on = set()          # models that never switch on at all
        self.models_unreadable = False
        self._armed = False

    def _switch(self, model, on):
        """Apply a state change the way the controller does (quirk included)."""
        entry = self.overlay_state.setdefault(model, {})
        if on and model in self.never_on:
            return
        if on and self._armed:
            self._armed = False        # the ignored first switch-on
            return
        entry["State"] = 1 if on else 0

    def handle(self, method, url, json=None, **_kwargs):
        path = url.split("/api", 1)[1] if "/api" in url else url
        self.calls.append((method, path, json))

        if path == "/playlists" and method == "GET":
            return _Response(sorted(self.playlists))
        if path == "/playlists/stop":
            self.status = {"status_name": "idle", "current_playlist": {"playlist": ""}}
            return _Response({"status": "OK"})
        m = re.fullmatch(r"/playlist/([^/]+)", path)
        if m:
            name = unquote(m.group(1))
            if method == "POST":
                self.playlists[name] = json
            elif method == "DELETE":
                self.playlists.pop(name, None)
            elif method == "GET":
                return _Response(self.playlists.get(name), 200 if name in self.playlists else 404)
            return _Response({"status": "OK"})
        m = re.fullmatch(r"/overlays/model/([^/]+)/(state|fill)", path)
        if m:
            model = unquote(m.group(1))
            if m.group(2) == "state":
                self._switch(model, (json or {}).get("State") == 1)
            else:
                self.overlay_state.setdefault(model, {}).update(json or {})
            return _Response({"status": "OK"})
        if path == "/overlays/models" and method == "GET":
            if self.models_unreadable:
                return _Response({}, 500)
            return _Response([{"Name": n, "isActive": self.overlay_state.get(n, {}).get("State", 0)}
                              for n in self.models])
        if path == "/schedule" and method == "GET":
            return _Response(list(self.schedule))
        if path == "/schedule" and method == "POST":
            self.schedule = list(json or [])
            return _Response({"status": "OK"})
        if path == "/schedule/reload":
            return _Response({"status": "OK"})
        if path == "/fppd/schedule":
            return _Response({"schedule": self.fppd_schedule})
        if path == "/fppd/status":
            return _Response(self.status)
        if path == "/command":
            args = (json or {}).get("args", [])
            if len(args) >= 3 and args[2] == "Stop Effects":
                for name in list(self.overlay_state):
                    self.overlay_state[name]["State"] = 0
                self._armed = self.ignore_first_on_after_stop
            elif len(args) >= 3 and (json or {}).get("command") == "Overlay Model Effect":
                for name in args[0].split(","):
                    self._switch(name, True)
            return _Response({"status": "OK"})
        return _Response({}, 404)

    def lit(self):
        """Overlay models currently switched on."""
        return {m for m, s in self.overlay_state.items() if s.get("State") == 1}


@pytest.fixture(autouse=True)
def instant_activation_checks(monkeypatch):
    """The read-back after switching models on waits for fppd; tests do not."""
    from app import fpp_api
    monkeypatch.setattr(fpp_api, "_ACTIVATION_SETTLE", 0)


@pytest.fixture
def fpp():
    fake = FakeFPP()
    patches = [
        mock.patch(f"requests.{verb}", side_effect=lambda url, _v=verb.upper(), **kw: fake.handle(_v, url, **kw))
        for verb in ("get", "post", "put", "delete")
    ]
    for p in patches:
        p.start()
    yield fake
    for p in patches:
        p.stop()


@pytest.fixture
def app_factory():
    """Build isolated apps (own in-memory DB); use ``provisioned=False`` for first-run."""
    from app import create_app

    def make(provisioned=True):
        app = create_app()
        app.config["ADMIN_PASSWORD_HASH"] = "x" if provisioned else ""
        return app

    return make
